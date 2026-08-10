<!-- mcp-name: io.github.es617/cc-tap -->

# cc-tap

[![MCP](https://img.shields.io/badge/MCP-compatible-blue)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Experimental MCP server that lets Claude Desktop see into and interact with Claude Code [Remote Control](https://code.claude.com/docs/en/remote-control) sessions.

> **Example:** Ask Claude Desktop *"what's my Code session working on?"* and get a real answer.

![cc-tap demo](https://raw.githubusercontent.com/es617/cc-tap/main/docs/demo.gif)


## Why this exists

Claude Desktop and Claude Code are separate worlds. Both build up rich context on your project, but neither can see what the other is doing. A design idea on Desktop that should drive an implementation in Code, or an implementation detail in Code that the Desktop conversation needs; both require you to copy-paste between windows and re-explain context that already exists.

Claude Code's [Remote Control](https://code.claude.com/docs/en/remote-control) lets you drive a session from another device, but that's still you driving the same session. cc-tap is for the other axis: letting a different agent (Claude Desktop, or another Claude Code instance) read and interact with a running CC session through MCP.

## Who is this for

- Developers who use both Claude Desktop and Claude Code
- Anyone who wants to monitor or interact with CC sessions programmatically
- Multi-agent workflows where one Claude instance needs to coordinate with another

## Quickstart

```bash
pip install cc-tap
```

Requires an active Claude Code login (`claude /login`) and [Remote Control](https://code.claude.com/docs/en/remote-control) enabled.

### Claude Code

```bash
claude mcp add cc-tap -- cc_tap
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cc-tap": {
      "command": "cc_tap",
      "args": []
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `list_sessions` | List all CC sessions, with repo and status inline (filter by `status_filter`, `repo`) |
| `get_session_info` | Get details about a specific session (`include_mcp_config` for the full connector blob) |
| `read_session` | Read recent conversation from a session |
| `get_session_events` | Get raw events, optionally filtered by type |
| `send_message` | Send a message to a CC session (fire and forget) |
| `send_and_wait` | Send a message and wait for the full response |

## Remote access over HTTPS

By default cc-tap speaks **stdio**, so only a client on the same machine can reach it. To use it from a client that runs in Anthropic's cloud — voice mode, the mobile app, scheduled tasks — it needs a public HTTPS endpoint speaking MCP streamable HTTP:

```bash
export MCP_SECRET=$(openssl rand -hex 32)
export CC_TAP_PUBLIC_URL=https://cc-tap.example.com
cc_tap --transport http --host 0.0.0.0 --port 8787
```

Put TLS in front of it (any reverse proxy). Then in **claude.ai → Settings → Connectors → Add custom connector**, use `https://cc-tap.example.com/mcp`.

| Path | Auth | Purpose |
|------|------|---------|
| `/mcp` | required | MCP streamable HTTP endpoint |
| `/health` | open | liveness probe |
| `/consent` | — | approval form; asks for `MCP_SECRET` |
| `/authorize`, `/token`, `/register` | — | OAuth 2.1 endpoints |
| `/.well-known/oauth-authorization-server` | open | AS metadata (RFC 8414) |
| `/.well-known/oauth-protected-resource/mcp` | open | RS metadata (RFC 9728) |

### Authentication

The connector form takes only a name, a URL, and optional OAuth client credentials — there is no field for a bearer token or custom header. So access is gated by OAuth 2.1 (authorization code + PKCE), and both connection styles work:

- **Leave Client ID / Client Secret blank** — Claude registers itself via Dynamic Client Registration.
- **Paste them** — a fixed ID and secret derived from `MCP_SECRET`, printed at startup.

Either way the browser lands on a consent page that requires `MCP_SECRET` before a code is issued. **That consent step is the real access gate.** With dynamic registration enabled, anyone who can reach the URL can register a client, so approving automatically would leave the endpoint open to whoever knows the address. `CC_TAP_OAUTH_AUTO_APPROVE=1` disables the check — don't, unless the endpoint is already private.

Redirect URIs are restricted to `claude.ai` and `claude.com` over HTTPS. Issued tokens are persisted (`CC_TAP_TOKEN_STORE`, mode 0600) so connections survive a restart.

See [`.env.example`](.env.example) for every variable.

### Deploying

The important constraint is credentials, not transport. cc-tap resolves the Anthropic token from `CLAUDE_CODE_OAUTH_TOKEN`, then the macOS Keychain, then `~/.claude/.credentials.json` — and **that token expires and is refreshed by the running Claude Code CLI, not by cc-tap**. A token pinned into an env var will expire and stay expired; cc-tap re-reads the credentials file when the API returns 401, so it must see the *live* file.

That means running cc-tap **as the same user, on the same machine, as the Claude Code that keeps those credentials fresh**. A systemd user service is the simplest way:

```ini
# ~/.config/systemd/user/cc-tap.service
[Service]
Environment=CC_TAP_TRANSPORT=http
Environment=CC_TAP_HOST=0.0.0.0
Environment=CC_TAP_PUBLIC_URL=https://cc-tap.example.com
EnvironmentFile=%h/.config/cc-tap.env
ExecStart=%h/.local/bin/cc_tap
Restart=on-failure
```

[`Dockerfile`](Dockerfile) and [`docker-compose.yml`](docker-compose.yml) are provided for container deployments. Mount the whole `~/.claude` **directory** read-only rather than the `.credentials.json` file — a single-file bind mount pins an inode, so a refresh that replaces the file would leave the container reading a stale copy — and build with `APP_UID` matching the user that owns it (the file is mode 0600).

## How it works

cc-tap reads your Claude Code OAuth credentials (from macOS Keychain or `~/.claude/.credentials.json`) and talks to the same API that Claude Code's [Remote Control](https://code.claude.com/docs/en/remote-control) web UI uses. No additional authentication needed.

Sessions are accessed via HTTP polling (~1.5s latency). Messages you send appear in the target CC session as if typed by the user.

## Limitations

- **Tool approval** — the session runtime only picks up approvals via WebSocket (behind Cloudflare bot protection). You can see pending tool requests via `send_and_wait`, but must approve them in the CC terminal or claude.ai/code web UI.
- **Not real-time** — uses HTTP polling, not WebSocket streaming. ~1.5s latency.
- **Undocumented API** — uses internal Anthropic endpoints that may change without notice.
- **Credentials are local** — cc-tap must run as the user whose Claude Code keeps `~/.claude/.credentials.json` fresh. It can be [reached remotely over HTTPS](#remote-access-over-https), but it cannot be deployed somewhere that user's live credentials are not readable.

## Protocol

See [PROTOCOL.md](https://github.com/es617/cc-tap/blob/main/PROTOCOL.md) for the reverse-engineered Claude Code Remote session API documentation.

## Development

```bash
git clone https://github.com/es617/cc-tap.git
cd cc-tap
pip install -e ".[dev,test]"
pre-commit install
pytest
```

## Disclaimer

This project is an **experimental research tool** for personal and educational use. It interacts with undocumented, internal Anthropic APIs that are not part of any public or supported API surface. These endpoints may change, break, or be removed at any time without notice.

This project is **not affiliated with, endorsed by, or supported by Anthropic**. Use it at your own risk. The authors assume no responsibility for any consequences of using this tool, including but not limited to account restrictions, data loss, or service disruption.

By using this tool you acknowledge that you are responsible for compliance with Anthropic's [Terms of Service](https://www.anthropic.com/terms) and [Acceptable Use Policy](https://www.anthropic.com/aup).

## License

[MIT](https://github.com/es617/cc-tap/blob/main/LICENSE)
