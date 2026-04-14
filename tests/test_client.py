"""Tests for the CCR client."""

from cc_tap.client import CCRSession, extract_text


class TestExtractText:
    def test_string_content(self):
        assert extract_text("hello") == "hello"

    def test_empty_string(self):
        assert extract_text("") == ""

    def test_content_blocks(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]
        assert extract_text(content) == "hello\nworld"

    def test_mixed_blocks(self):
        content = [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "bash"},
        ]
        assert extract_text(content) == "hello"

    def test_empty_list(self):
        assert extract_text([]) == ""

    def test_none(self):
        assert extract_text(None) == ""


class TestCCRSession:
    def test_session_id_with_prefix(self):
        s = CCRSession(
            id="session_abc",
            title="",
            status="",
            connection_status="",
            created_at="",
            updated_at="",
        )
        assert s.session_id == "session_abc"

    def test_session_id_without_prefix(self):
        s = CCRSession(id="abc", title="", status="", connection_status="", created_at="", updated_at="")
        assert s.session_id == "session_abc"

    def test_cse_id(self):
        s = CCRSession(
            id="session_abc",
            title="",
            status="",
            connection_status="",
            created_at="",
            updated_at="",
        )
        assert s.cse_id == "cse_abc"
