"""MCP server exposing Claude Code session tools."""

import argparse
import json
import logging
import os
import sys
import threading
import time

import requests as req_lib
from mcp.server.fastmcp import FastMCP

from cc_tap.auth import load_oauth_token, load_org_uuid
from cc_tap.client import CCRClient, extract_text

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = "Bridge to Claude Code sessions — list, read, and interact with active sessions"

_client: CCRClient | None = None
_client_lock = threading.Lock()


def _build_client() -> CCRClient:
    token_data = load_oauth_token()
    if not token_data:
        msg = "No Claude Code credentials found. Run 'claude /login' first."
        raise RuntimeError(msg)
    access_token = token_data["accessToken"]
    org_uuid = load_org_uuid(access_token)
    if not org_uuid:
        msg = "Could not determine organization UUID."
        raise RuntimeError(msg)
    return CCRClient(access_token=access_token, org_uuid=org_uuid)


def _get_client() -> CCRClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = _build_client()
    return _client


def _reset_client() -> None:
    """Clear cached client so next call re-reads credentials."""
    global _client
    with _client_lock:
        _client = None


def _with_retry(fn):
    """Call fn(client). On 401, refresh credentials and retry once."""
    client = _get_client()
    try:
        return fn(client)
    except req_lib.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            logger.info("Got 401, refreshing credentials")
            _reset_client()
            client = _get_client()
            return fn(client)
        raise


def list_sessions(status_filter: str = "", repo: str = "") -> str:
    """List all Claude Code sessions, with repo and status shown inline.

    Args:
        status_filter: Optional filter — 'active' (non-archived), 'running', 'idle', or '' for all.
        repo: Optional repo filter, matched case-insensitively as a substring
            (e.g. 'sigma' matches 'midt-bg/sigma'). Empty for all repos.
    """
    sessions = _with_retry(lambda c: c.list_sessions())
    total = len(sessions)

    if status_filter == "active":
        sessions = [s for s in sessions if s.status != "archived"]
    elif status_filter:
        sessions = [s for s in sessions if s.status == status_filter]

    if repo:
        needle = repo.lower()
        sessions = [s for s in sessions if needle in s.repo.lower()]

    shown = sessions[:30]
    header = f"Found {len(sessions)} session(s)"
    if len(sessions) != total:
        header += f" (of {total} total)"
    lines = [header + ":\n"]

    for s in shown:
        bucket = s.status_bucket or s.status
        where = s.repo or "no repo"
        lines.append(f"- [{bucket}] {where} — {s.title}")
        if s.status_detail:
            lines.append(f"    {s.status_detail}")
        lines.append(f"    id: {s.id}")
    if len(sessions) > len(shown):
        lines.append(f"\n... and {len(sessions) - len(shown)} more")
    return "\n".join(lines)


def get_session_info(session_id: str, include_mcp_config: bool = False) -> str:
    """Get details about a specific Claude Code session.

    Args:
        session_id: The session ID (with or without 'session_' prefix).
        include_mcp_config: Include the session's full MCP connector config.
            Off by default — it is a large repeated blob (typically ~70% of the
            payload) and is almost never what you want.
    """
    s = _with_retry(lambda c: c.get_session(session_id))
    if not include_mcp_config:
        ctx = s.get("session_context")
        if isinstance(ctx, dict) and "mcp_config" in ctx:
            s = dict(s)
            trimmed = {k: v for k, v in ctx.items() if k != "mcp_config"}
            trimmed["mcp_config"] = "<omitted — pass include_mcp_config=true to see it>"
            s["session_context"] = trimmed
    return json.dumps(s, indent=2)


def read_session(session_id: str, last_n: int = 20) -> str:
    """Read recent conversation from a Claude Code session.

    Args:
        session_id: The session ID.
        last_n: Number of recent events to return (default 20).
    """
    events = _with_retry(lambda c: c.get_events(session_id))

    conv = [e for e in events if e["type"] in ("user", "assistant", "result")]
    recent = conv[-last_n:]

    lines = [f"Session has {len(events)} total events, showing last {len(recent)} conversation events:\n"]
    for ev in recent:
        if ev["type"] == "user":
            text = extract_text(ev.get("message", {}).get("content", ""))
            lines.append(f"[USER] {text}")
        elif ev["type"] == "assistant":
            text = extract_text(ev.get("message", {}).get("content", []))
            if text:
                lines.append(f"[ASSISTANT] {text}")
        elif ev["type"] == "result":
            cost = ev.get("total_cost_usd", 0)
            tokens_in = ev.get("usage", {}).get("input_tokens", 0)
            tokens_out = ev.get("usage", {}).get("output_tokens", 0)
            lines.append(f"[RESULT] cost=${cost:.4f} in={tokens_in} out={tokens_out}")
    return "\n\n".join(lines)


def get_session_events(session_id: str, event_types: str = "", last_n: int = 50) -> str:
    """Get raw events from a session, optionally filtered by type.

    Args:
        session_id: The session ID.
        event_types: Comma-separated types to filter (e.g. 'user,assistant'). Empty for all.
        last_n: Number of recent events to return (default 50).
    """
    events = _with_retry(lambda c: c.get_events(session_id))

    if event_types:
        type_set = {t.strip() for t in event_types.split(",")}
        events = [e for e in events if e["type"] in type_set]

    recent = events[-last_n:]
    return json.dumps(recent, indent=2, default=str)


def send_message(session_id: str, message: str) -> str:
    """Send a message to a Claude Code session.

    Args:
        session_id: The session ID to send to.
        message: The message text to send.
    """
    result = _with_retry(lambda c: c.send_message(session_id, message))
    return json.dumps(result, indent=2)


def send_and_wait(session_id: str, message: str, timeout: int = 120, poll_interval: float = 1.5) -> str:
    """Send a message to a Claude Code session and wait for the full response.

    Polls until the session produces a result event (turn complete), then returns
    the assistant's response. Use this when you need the other session's answer.

    Args:
        session_id: The session ID to send to.
        message: The message text to send.
        timeout: Max seconds to wait for a response (default 120).
        poll_interval: Seconds between polls (default 1.5).
    """
    # Initial calls use retry; polling loop uses client directly
    events_before = _with_retry(lambda c: c.get_events(session_id))
    baseline = len(events_before)

    _with_retry(lambda c: c.send_message(session_id, message))
    client = _get_client()
    client.send_presence(session_id)

    deadline = time.time() + timeout
    collected_text: list[str] = []

    while time.time() < deadline:
        time.sleep(poll_interval)
        events = client.get_events(session_id)

        if len(events) <= baseline:
            continue

        new_events = events[baseline:]
        baseline = len(events)

        tool_requests: list[dict] = []

        for ev in new_events:
            if ev["type"] == "assistant":
                text = extract_text(ev.get("message", {}).get("content", []))
                if text:
                    collected_text.append(text)
            elif ev["type"] == "control_request":
                req = ev.get("request", {})
                tool_requests.append(
                    {
                        "tool_name": req.get("tool_name", "?"),
                        "description": req.get("description", ""),
                        "request_id": req.get("tool_use_id") or ev.get("request_id", ""),
                        "input": req.get("input", {}),
                    }
                )
            elif ev["type"] == "result":
                result: dict = {"status": "complete", "response": "\n\n".join(collected_text)}
                cost = ev.get("total_cost_usd", 0)
                if cost:
                    result["cost_usd"] = cost
                return json.dumps(result, indent=2)

        if tool_requests:
            return json.dumps(
                {
                    "status": "waiting_for_tool_approval",
                    "response_so_far": "\n\n".join(collected_text),
                    "pending_tool_requests": tool_requests,
                },
                indent=2,
            )

    return json.dumps(
        {
            "status": "timeout",
            "response_so_far": "\n\n".join(collected_text),
            "message": f"No result event within {timeout}s. Session may still be processing.",
        },
        indent=2,
    )


TOOLS = (
    list_sessions,
    get_session_info,
    read_session,
    get_session_events,
    send_message,
    send_and_wait,
)


def build_mcp(**kwargs) -> FastMCP:
    """Create a FastMCP instance with all cc-tap tools registered.

    Auth has to be supplied at construction time (FastMCP validates it there
    and derives its token verifier from the provider), so the HTTP path builds
    its own instance rather than mutating a shared one.
    """
    server = FastMCP("cc-tap", instructions=SERVER_INSTRUCTIONS, **kwargs)
    for fn in TOOLS:
        server.tool()(fn)
    return server


#: Module-level instance used by the stdio transport (and by `mcp dev`).
mcp = build_mcp()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def main():
    parser = argparse.ArgumentParser(description="cc-tap: MCP server for Claude Code sessions")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("CC_TAP_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio; env: CC_TAP_TRANSPORT)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("CC_TAP_HOST", "127.0.0.1"),
        help="Bind address for --transport http (default: 127.0.0.1; env: CC_TAP_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CC_TAP_PORT", "8787")),
        help="Bind port for --transport http (default: 8787; env: CC_TAP_PORT)",
    )
    parser.add_argument(
        "--public-url",
        default=os.environ.get("CC_TAP_PUBLIC_URL"),
        help=(
            "Public HTTPS base URL clients reach this server on, used as the OAuth "
            "issuer and resource identifier (env: CC_TAP_PUBLIC_URL)"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("CC_TAP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # Imported here so the stdio path keeps working without the HTTP extras.
    from cc_tap.http_app import run_http

    run_http(host=args.host, port=args.port, public_url=args.public_url)


if __name__ == "__main__":
    main()
