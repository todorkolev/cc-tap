# Changelog

## [Unreleased]

### Added
- Streamable-HTTP transport: `--transport http` / `CC_TAP_TRANSPORT`, bind via
  `CC_TAP_HOST` / `CC_TAP_PORT`, MCP served at `/mcp`. stdio remains the default.
- Unauthenticated `/health` endpoint reporting whether Claude Code credentials are readable.
- OAuth 2.1 in front of `/mcp`: authorization code + PKCE, Dynamic Client
  Registration, and fixed client credentials derived from `MCP_SECRET`. Redirect
  URIs restricted to claude.ai / claude.com; issued tokens persisted to disk.
- Consent page requiring `MCP_SECRET` before an authorization code is issued.
- `repo` filter on `list_sessions`; `include_mcp_config` on `get_session_info`.
- `Dockerfile`, `docker-compose.yml`, `.env.example`, and a pinned `requirements.lock`.

### Changed
- `list_sessions` now shows `repo`, `status_bucket` and `post_turn_summary.status_detail`
  inline. These already come back from `GET /v1/sessions`, so answering "what is
  blocked on repo X?" no longer costs one `get_session_info` per session.
- `get_session_info` omits `session_context.mcp_config` unless asked for it —
  measured at ~70% of the payload (~2400 tokens down to ~440).
- Pinned `mcp>=1.29,<2`: mcp 2.0 removed `mcp.server.fastmcp` (renamed to
  `mcp.server.mcpserver`), so the previous `mcp>=1.0` no longer installed.

## [0.1.0] - Unreleased

### Added
- MCP server with session tools: list, read, send message, approve/deny tools
- Auto-authentication via macOS Keychain and plaintext credential fallback
- Protocol documentation (PROTOCOL.md)
- Reference Python client (scripts/ccr_client.py)
