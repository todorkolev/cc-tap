"""MCP server exposing Claude Code session tools."""

import argparse
import json
import logging
import time

from mcp.server.fastmcp import FastMCP

from cc_tap.auth import load_oauth_token, load_org_uuid
from cc_tap.client import CCRClient, extract_text

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "cc-tap",
    instructions="Bridge to Claude Code sessions — list, read, and interact with active sessions",
)

# Module-level client, initialized on startup
_client: CCRClient | None = None


def _get_client() -> CCRClient:
    global _client
    if _client is None:
        token_data = load_oauth_token()
        if not token_data:
            msg = "No Claude Code credentials found. Run 'claude /login' first."
            raise RuntimeError(msg)
        access_token = token_data["accessToken"]
        org_uuid = load_org_uuid(access_token)
        if not org_uuid:
            msg = "Could not determine organization UUID."
            raise RuntimeError(msg)
        _client = CCRClient(access_token=access_token, org_uuid=org_uuid)
    return _client


@mcp.tool()
def list_sessions(status_filter: str = "") -> str:
    """List all Claude Code sessions.

    Args:
        status_filter: Optional filter — 'active' (non-archived), 'running', 'idle', or '' for all.
    """
    client = _get_client()
    sessions = client.list_sessions()

    if status_filter == "active":
        sessions = [s for s in sessions if s.status != "archived"]
    elif status_filter:
        sessions = [s for s in sessions if s.status == status_filter]

    lines = [f"Found {len(sessions)} session(s):\n"]
    for s in sessions[:30]:
        lines.append(f"- [{s.status}] {s.title}  (id: {s.id})")
    if len(sessions) > 30:
        lines.append(f"  ... and {len(sessions) - 30} more")
    return "\n".join(lines)


@mcp.tool()
def get_session_info(session_id: str) -> str:
    """Get details about a specific Claude Code session.

    Args:
        session_id: The session ID (with or without 'session_' prefix).
    """
    client = _get_client()
    s = client.get_session(session_id)
    return json.dumps(s, indent=2)


@mcp.tool()
def read_session(session_id: str, last_n: int = 20) -> str:
    """Read recent conversation from a Claude Code session.

    Args:
        session_id: The session ID.
        last_n: Number of recent events to return (default 20).
    """
    client = _get_client()
    events = client.get_events(session_id)

    # Filter to user/assistant/result events
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


@mcp.tool()
def get_session_events(session_id: str, event_types: str = "", last_n: int = 50) -> str:
    """Get raw events from a session, optionally filtered by type.

    Args:
        session_id: The session ID.
        event_types: Comma-separated types to filter (e.g. 'user,assistant'). Empty for all.
        last_n: Number of recent events to return (default 50).
    """
    client = _get_client()
    events = client.get_events(session_id)

    if event_types:
        type_set = {t.strip() for t in event_types.split(",")}
        events = [e for e in events if e["type"] in type_set]

    recent = events[-last_n:]
    return json.dumps(recent, indent=2, default=str)


@mcp.tool()
def send_message(session_id: str, message: str) -> str:
    """Send a message to a Claude Code session.

    Args:
        session_id: The session ID to send to.
        message: The message text to send.
    """
    client = _get_client()
    result = client.send_message(session_id, message)
    return json.dumps(result, indent=2)


@mcp.tool()
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
    client = _get_client()

    # Get baseline event count before sending
    events_before = client.get_events(session_id)
    baseline = len(events_before)

    # Send the message
    client.send_message(session_id, message)
    client.send_presence(session_id)

    # Poll for new events until we see a result
    deadline = time.time() + timeout
    collected_text: list[str] = []
    tool_requests: list[dict] = []

    while time.time() < deadline:
        time.sleep(poll_interval)
        events = client.get_events(session_id)

        if len(events) <= baseline:
            continue

        new_events = events[baseline:]
        baseline = len(events)

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
                # Turn complete
                result = {"status": "complete", "response": "\n\n".join(collected_text)}
                if tool_requests:
                    result["pending_tool_requests"] = tool_requests
                cost = ev.get("total_cost_usd", 0)
                if cost:
                    result["cost_usd"] = cost
                return json.dumps(result, indent=2)

        # If there are pending tool requests, return early so the caller can decide
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


@mcp.tool()
def approve_tool(session_id: str, request_id: str, tool_input: str = "{}") -> str:
    """Approve a pending tool use in a Claude Code session.

    Args:
        session_id: The session ID.
        request_id: The tool_use_id from the control_request event.
        tool_input: JSON string of the tool input to approve (usually from the control_request).
    """
    client = _get_client()
    updated_input = json.loads(tool_input) if tool_input else None
    result = client.post_control_response(session_id, request_id, "allow", updated_input=updated_input)
    return json.dumps(result, indent=2)


@mcp.tool()
def deny_tool(session_id: str, request_id: str, reason: str = "Denied by user") -> str:
    """Deny a pending tool use in a Claude Code session.

    Args:
        session_id: The session ID.
        request_id: The tool_use_id from the control_request event.
        reason: Reason for denial.
    """
    client = _get_client()
    result = client.post_control_response(session_id, request_id, "deny", message=reason)
    return json.dumps(result, indent=2)


def main():
    parser = argparse.ArgumentParser(description="cc-tap: MCP server for Claude Code sessions")
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="MCP transport",
    )
    args = parser.parse_args()

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
