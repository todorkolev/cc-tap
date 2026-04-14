<!-- mcp-name: io.github.es617/cc-tap -->

# cc-tap

[![MCP](https://img.shields.io/badge/MCP-compatible-blue)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

MCP server that lets Claude Desktop see into and interact with Claude Code sessions.

## Why this exists

Claude Code and Claude Desktop are separate worlds. You can't see what a CC session is doing from Desktop, and you can't have two CC instances coordinate. cc-tap bridges that gap by exposing CC sessions as MCP tools.

## Who is this for

- Developers who use both Claude Desktop and Claude Code
- Anyone who wants to monitor or interact with CC sessions programmatically
- Multi-agent workflows where one Claude instance needs to coordinate with another

## Quickstart

```bash
pip install cc-tap
```

Requires an active Claude Code login (`claude /login`).

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
| `list_sessions` | List all CC sessions (optionally filter by status) |
| `get_session_info` | Get details about a specific session |
| `read_session` | Read recent conversation from a session |
| `get_session_events` | Get raw events, optionally filtered by type |
| `send_message` | Send a message to a CC session (fire and forget) |
| `send_and_wait` | Send a message and wait for the full response |
| `approve_tool` | Approve a pending tool use request |
| `deny_tool` | Deny a pending tool use request |

## How it works

cc-tap reads your Claude Code OAuth credentials (from macOS Keychain or `~/.claude/.credentials.json`) and talks to the same API that the claude.ai/code web UI uses. No additional authentication needed.

Sessions are accessed via HTTP polling (~1.5s latency). Messages you send appear in the target CC session as if typed by the user.

## Limitations

- **Tool approval** — `approve_tool` / `deny_tool` post events via HTTP, but the session runtime only picks up approvals via WebSocket (which is behind Cloudflare bot protection). You can see pending tool requests, but must approve them in the CC terminal or claude.ai/code web UI.
- **Not real-time** — uses HTTP polling, not WebSocket streaming. ~1.5s latency.
- **Undocumented API** — uses internal Anthropic endpoints that may change without notice.
- **Local only** — reads credentials from the local machine. Can't be deployed as a remote service.

## Protocol

See [PROTOCOL.md](PROTOCOL.md) for the reverse-engineered Claude Code Remote session API documentation.

## Development

```bash
git clone https://github.com/es617/cc-tap.git
cd cc-tap
pip install -e ".[dev,test]"
pre-commit install
pytest
```

## License

MIT
