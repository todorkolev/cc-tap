"""Tests for the MCP tool layer."""

import json
import pathlib
from typing import ClassVar
from unittest.mock import MagicMock, patch

import anyio
import pytest

from cc_tap.client import CCRSession
from cc_tap.server import (
    TOOLS,
    build_mcp,
    get_session_events,
    get_session_info,
    list_sessions,
    read_session,
    send_and_wait,
)


def session(**overrides) -> CCRSession:
    base = {
        "id": "session_abc12345",
        "title": "Some title",
        "status": "idle",
        "connection_status": "connected",
        "created_at": "",
        "updated_at": "",
        "repo": "acme/widgets",
        "status_bucket": "blocked",
        "status_detail": "waiting on approval",
    }
    base.update(overrides)
    return CCRSession(**base)


@pytest.fixture
def fake_client():
    client = MagicMock()
    with patch("cc_tap.server._get_client", return_value=client):
        yield client


class TestListSessions:
    async def test_shows_repo_bucket_and_detail_inline(self, fake_client):
        fake_client.list_sessions.return_value = [session()]
        out = await list_sessions()
        assert "acme/widgets" in out
        assert "[blocked]" in out
        assert "waiting on approval" in out
        assert "session_abc12345" in out

    async def test_one_api_call_only(self, fake_client):
        fake_client.list_sessions.return_value = [session() for _ in range(5)]
        await list_sessions()
        assert fake_client.list_sessions.call_count == 1
        # the whole point: no per-session follow-up
        assert fake_client.get_session.call_count == 0

    async def test_repo_filter_is_substring_and_case_insensitive(self, fake_client):
        fake_client.list_sessions.return_value = [
            session(id="session_a1111111", repo="midt-bg/sigma"),
            session(id="session_b2222222", repo="acme/widgets"),
        ]
        out = await list_sessions(repo="SIGMA")
        assert "midt-bg/sigma" in out
        assert "acme/widgets" not in out
        assert "Found 1 session(s) (of 2 total)" in out

    async def test_repo_filter_no_matches(self, fake_client):
        fake_client.list_sessions.return_value = [session()]
        assert "Found 0 session(s)" in await list_sessions(repo="nothing-matches")

    async def test_status_filter_active_excludes_archived(self, fake_client):
        fake_client.list_sessions.return_value = [
            session(id="session_a1111111", status="idle"),
            session(id="session_b2222222", status="archived"),
        ]
        out = await list_sessions(status_filter="active")
        assert "session_a1111111" in out
        assert "session_b2222222" not in out

    async def test_filters_combine(self, fake_client):
        fake_client.list_sessions.return_value = [
            session(id="session_a1111111", status="idle", repo="acme/widgets"),
            session(id="session_b2222222", status="archived", repo="acme/widgets"),
            session(id="session_c3333333", status="idle", repo="other/thing"),
        ]
        out = await list_sessions(status_filter="active", repo="acme")
        assert "session_a1111111" in out
        assert "session_b2222222" not in out
        assert "session_c3333333" not in out

    async def test_missing_repo_is_labelled(self, fake_client):
        fake_client.list_sessions.return_value = [session(repo="")]
        assert "no repo" in await list_sessions()

    async def test_falls_back_to_status_when_no_bucket(self, fake_client):
        fake_client.list_sessions.return_value = [session(status_bucket="", status="running")]
        assert "[running]" in await list_sessions()

    async def test_detail_line_omitted_when_empty(self, fake_client):
        fake_client.list_sessions.return_value = [session(status_detail="")]
        out = await list_sessions()
        assert "acme/widgets" in out
        # no stray blank bullet line for the missing detail
        assert "\n    \n" not in out

    async def test_caps_output_and_says_so(self, fake_client):
        fake_client.list_sessions.return_value = [session(id=f"session_x{i:07d}") for i in range(40)]
        out = await list_sessions()
        assert "Found 40 session(s)" in out
        assert "... and 10 more" in out


class TestGetSessionInfo:
    RAW: ClassVar[dict] = {
        "id": "session_abc12345",
        "title": "t",
        "session_context": {
            "cwd": "/home/user/work",
            "model": "claude-opus-5",
            "mcp_config": {"servers": ["a"] * 500},
        },
    }

    async def test_strips_mcp_config_by_default(self, fake_client):
        fake_client.get_session.return_value = self.RAW
        out = json.loads(await get_session_info("session_abc12345"))
        assert out["session_context"]["mcp_config"] == "<omitted — pass include_mcp_config=true to see it>"
        assert out["session_context"]["cwd"] == "/home/user/work"

    async def test_includes_mcp_config_on_request(self, fake_client):
        fake_client.get_session.return_value = self.RAW
        out = json.loads(await get_session_info("session_abc12345", include_mcp_config=True))
        assert out["session_context"]["mcp_config"] == {"servers": ["a"] * 500}

    async def test_stripping_is_much_smaller(self, fake_client):
        fake_client.get_session.return_value = self.RAW
        slim = await get_session_info("session_abc12345")
        full = await get_session_info("session_abc12345", include_mcp_config=True)
        assert len(slim) * 4 < len(full)

    async def test_does_not_mutate_source_dict(self, fake_client):
        raw = json.loads(json.dumps(self.RAW))
        fake_client.get_session.return_value = raw
        await get_session_info("session_abc12345")
        assert raw["session_context"]["mcp_config"] == {"servers": ["a"] * 500}

    async def test_handles_missing_session_context(self, fake_client):
        fake_client.get_session.return_value = {"id": "session_abc12345"}
        assert json.loads(await get_session_info("session_abc12345")) == {"id": "session_abc12345"}

    async def test_handles_context_without_mcp_config(self, fake_client):
        fake_client.get_session.return_value = {"id": "x", "session_context": {"cwd": "/w"}}
        out = json.loads(await get_session_info("session_abc12345"))
        assert out["session_context"] == {"cwd": "/w"}


class TestServerWiring:
    def test_all_six_tools_registered(self):
        assert len(TOOLS) == 6

    async def test_build_mcp_registers_every_tool(self):
        server = build_mcp()
        names = {t.name for t in await server.list_tools()}
        assert names == {fn.__name__ for fn in TOOLS}

    async def test_new_arguments_are_exposed(self):
        server = build_mcp()
        tools = {t.name: t for t in await server.list_tools()}
        assert "repo" in tools["list_sessions"].inputSchema["properties"]
        assert "include_mcp_config" in tools["get_session_info"].inputSchema["properties"]

    async def test_include_mcp_config_defaults_false(self):
        server = build_mcp()
        tools = {t.name: t for t in await server.list_tools()}
        prop = tools["get_session_info"].inputSchema["properties"]["include_mcp_config"]
        assert prop.get("default") is False


class TestToolsDoNotBlockTheEventLoop:
    """FastMCP calls a sync tool fn directly on the event loop (no thread
    offload), so a blocking tool stalls the whole server — including the
    transport that has to deliver its own response. send_and_wait blocks for up
    to its timeout, which made it unusable over HTTP.
    """

    def test_every_tool_is_a_coroutine_function(self):
        import inspect

        not_async = [fn.__name__ for fn in TOOLS if not inspect.iscoroutinefunction(fn)]
        assert not_async == [], f"these would block the event loop: {not_async}"

    def test_server_module_does_not_sleep_synchronously(self):
        """time.sleep in a tool would block every other request."""
        src = (pathlib.Path(__file__).parent.parent / "cc_tap" / "server.py").read_text()
        assert "time.sleep(" not in src
        assert "anyio.sleep(" in src

    async def test_send_and_wait_yields_between_polls(self, fake_client):
        """The poll loop must await, so other requests can be served."""
        fake_client.get_events.side_effect = [
            [{"uuid": "e0", "type": "user"}],  # events_before
            [],  # first poll: nothing yet
            [
                {"uuid": "e1", "type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
                {"uuid": "e2", "type": "result", "total_cost_usd": 0.01},
            ],
        ]
        fake_client.send_message.return_value = {"ok": True}

        ticks = []
        real_sleep = anyio.sleep

        async def counting_sleep(d):
            ticks.append(d)
            await real_sleep(0)

        with patch("cc_tap.server.anyio.sleep", counting_sleep):
            out = json.loads(await send_and_wait("session_abc12345", "ping", timeout=10, poll_interval=0.01))

        assert out["status"] == "complete"
        assert out["response"] == "hi"
        assert len(ticks) == 2, "expected one awaited sleep per poll"

    async def test_send_and_wait_reports_pending_tool_approval(self, fake_client):
        fake_client.get_events.side_effect = [
            [{"uuid": "e0", "type": "user"}],
            [{"uuid": "e1", "type": "control_request",
              "request": {"tool_name": "Bash", "description": "ls", "tool_use_id": "t1", "input": {}}}],
        ]
        fake_client.send_message.return_value = {"ok": True}
        with patch("cc_tap.server.anyio.sleep", _noop_sleep):
            out = json.loads(await send_and_wait("session_abc12345", "go", timeout=10, poll_interval=0.01))
        assert out["status"] == "waiting_for_tool_approval"
        assert out["pending_tool_requests"][0]["tool_name"] == "Bash"

    async def test_send_and_wait_polls_with_a_cursor(self, fake_client):
        """Polls must ask only for new events, not re-read the whole history."""
        fake_client.get_events.side_effect = [
            [{"uuid": "last-seen", "type": "user"}],
            [{"uuid": "e9", "type": "result"}],
        ]
        fake_client.send_message.return_value = {"ok": True}
        with patch("cc_tap.server.anyio.sleep", _noop_sleep):
            await send_and_wait("session_abc12345", "go", timeout=10, poll_interval=0.01)
        assert fake_client.get_events.call_args_list[-1].kwargs["after_id"] == "last-seen"

    async def test_send_and_wait_times_out_cleanly(self, fake_client):
        fake_client.get_events.return_value = []
        fake_client.send_message.return_value = {"ok": True}
        with patch("cc_tap.server.anyio.sleep", _noop_sleep):
            out = json.loads(await send_and_wait("session_abc12345", "go", timeout=-1, poll_interval=0.01))
        assert out["status"] == "timeout"


#: Captured before any patching: anyio is a shared module object, so patching
#: cc_tap.server.anyio.sleep patches anyio.sleep everywhere, and a helper that
#: called it again would recurse into itself.
_REAL_SLEEP = anyio.sleep


async def _noop_sleep(_d):
    await _REAL_SLEEP(0)


class TestReadToolsThroughTailCache:
    """read_session / get_session_events now serve the tail via the cache."""

    async def test_read_session_formats_returned_tail(self):
        events = [
            {"type": "user", "message": {"content": "hello"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi back"}]}},
            {"type": "result", "total_cost_usd": 0.02, "usage": {"input_tokens": 5, "output_tokens": 7}},
        ]
        with patch("cc_tap.server._recent_events", return_value=events):
            out = await read_session("session_abc12345", last_n=20)
        assert "[USER] hello" in out
        assert "[ASSISTANT] hi back" in out
        assert "cost=$0.0200" in out
        assert "newest last" in out

    async def test_read_session_returns_indexing_notice_while_warming(self):
        with patch("cc_tap.server._recent_events", return_value=None):
            out = await read_session("session_01MjkNQ3rJK1zBwkR1BurTfd")
        assert "indexed" in out.lower()
        assert "retry" in out.lower()
        assert "session_01MjkNQ3rJK1zBwkR1BurTfd" in out

    async def test_read_session_requests_conversation_types(self):
        captured = {}

        def fake(_sid, want, keep):
            captured["keep"] = keep
            return []

        with patch("cc_tap.server._recent_events", side_effect=fake):
            await read_session("session_abc12345")
        keep = captured["keep"]
        assert keep({"type": "assistant"}) and keep({"type": "result"})
        assert not keep({"type": "control_request"})

    async def test_get_session_events_returns_json_tail(self):
        events = [{"uuid": "e1", "type": "user"}, {"uuid": "e2", "type": "assistant"}]
        with patch("cc_tap.server._recent_events", return_value=events):
            out = await get_session_events("session_abc12345", last_n=50)
        assert json.loads(out) == events

    async def test_get_session_events_indexing_notice(self):
        with patch("cc_tap.server._recent_events", return_value=None):
            out = await get_session_events("session_abc12345")
        assert "retry" in out.lower()

    async def test_get_session_events_type_filter_passed_through(self):
        captured = {}

        def fake(_sid, want, keep):
            captured["keep"] = keep
            return []

        with patch("cc_tap.server._recent_events", side_effect=fake):
            await get_session_events("session_abc12345", event_types="user,result")
        keep = captured["keep"]
        assert keep({"type": "user"}) and keep({"type": "result"})
        assert not keep({"type": "assistant"})


class TestSidNormalization:
    def test_prefixes_collapse_to_one_key(self):
        from cc_tap.server import _normalize_sid

        assert _normalize_sid("abc123def") == "session_abc123def"
        assert _normalize_sid("session_abc123def") == "session_abc123def"
        assert _normalize_sid("cse_abc123def") == "session_abc123def"
