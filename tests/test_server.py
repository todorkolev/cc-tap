"""Tests for the MCP tool layer."""

import json
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from cc_tap.client import CCRSession
from cc_tap.server import TOOLS, build_mcp, get_session_info, list_sessions


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
    def test_shows_repo_bucket_and_detail_inline(self, fake_client):
        fake_client.list_sessions.return_value = [session()]
        out = list_sessions()
        assert "acme/widgets" in out
        assert "[blocked]" in out
        assert "waiting on approval" in out
        assert "session_abc12345" in out

    def test_one_api_call_only(self, fake_client):
        fake_client.list_sessions.return_value = [session() for _ in range(5)]
        list_sessions()
        assert fake_client.list_sessions.call_count == 1
        # the whole point: no per-session follow-up
        assert fake_client.get_session.call_count == 0

    def test_repo_filter_is_substring_and_case_insensitive(self, fake_client):
        fake_client.list_sessions.return_value = [
            session(id="session_a1111111", repo="midt-bg/sigma"),
            session(id="session_b2222222", repo="acme/widgets"),
        ]
        out = list_sessions(repo="SIGMA")
        assert "midt-bg/sigma" in out
        assert "acme/widgets" not in out
        assert "Found 1 session(s) (of 2 total)" in out

    def test_repo_filter_no_matches(self, fake_client):
        fake_client.list_sessions.return_value = [session()]
        assert "Found 0 session(s)" in list_sessions(repo="nothing-matches")

    def test_status_filter_active_excludes_archived(self, fake_client):
        fake_client.list_sessions.return_value = [
            session(id="session_a1111111", status="idle"),
            session(id="session_b2222222", status="archived"),
        ]
        out = list_sessions(status_filter="active")
        assert "session_a1111111" in out
        assert "session_b2222222" not in out

    def test_filters_combine(self, fake_client):
        fake_client.list_sessions.return_value = [
            session(id="session_a1111111", status="idle", repo="acme/widgets"),
            session(id="session_b2222222", status="archived", repo="acme/widgets"),
            session(id="session_c3333333", status="idle", repo="other/thing"),
        ]
        out = list_sessions(status_filter="active", repo="acme")
        assert "session_a1111111" in out
        assert "session_b2222222" not in out
        assert "session_c3333333" not in out

    def test_missing_repo_is_labelled(self, fake_client):
        fake_client.list_sessions.return_value = [session(repo="")]
        assert "no repo" in list_sessions()

    def test_falls_back_to_status_when_no_bucket(self, fake_client):
        fake_client.list_sessions.return_value = [session(status_bucket="", status="running")]
        assert "[running]" in list_sessions()

    def test_detail_line_omitted_when_empty(self, fake_client):
        fake_client.list_sessions.return_value = [session(status_detail="")]
        out = list_sessions()
        assert "acme/widgets" in out
        # no stray blank bullet line for the missing detail
        assert "\n    \n" not in out

    def test_caps_output_and_says_so(self, fake_client):
        fake_client.list_sessions.return_value = [session(id=f"session_x{i:07d}") for i in range(40)]
        out = list_sessions()
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

    def test_strips_mcp_config_by_default(self, fake_client):
        fake_client.get_session.return_value = self.RAW
        out = json.loads(get_session_info("session_abc12345"))
        assert out["session_context"]["mcp_config"] == "<omitted — pass include_mcp_config=true to see it>"
        assert out["session_context"]["cwd"] == "/home/user/work"

    def test_includes_mcp_config_on_request(self, fake_client):
        fake_client.get_session.return_value = self.RAW
        out = json.loads(get_session_info("session_abc12345", include_mcp_config=True))
        assert out["session_context"]["mcp_config"] == {"servers": ["a"] * 500}

    def test_stripping_is_much_smaller(self, fake_client):
        fake_client.get_session.return_value = self.RAW
        slim = get_session_info("session_abc12345")
        full = get_session_info("session_abc12345", include_mcp_config=True)
        assert len(slim) * 4 < len(full)

    def test_does_not_mutate_source_dict(self, fake_client):
        raw = json.loads(json.dumps(self.RAW))
        fake_client.get_session.return_value = raw
        get_session_info("session_abc12345")
        assert raw["session_context"]["mcp_config"] == {"servers": ["a"] * 500}

    def test_handles_missing_session_context(self, fake_client):
        fake_client.get_session.return_value = {"id": "session_abc12345"}
        assert json.loads(get_session_info("session_abc12345")) == {"id": "session_abc12345"}

    def test_handles_context_without_mcp_config(self, fake_client):
        fake_client.get_session.return_value = {"id": "x", "session_context": {"cwd": "/w"}}
        out = json.loads(get_session_info("session_abc12345"))
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
