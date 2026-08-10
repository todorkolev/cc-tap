"""Tests for the CCR client."""

from cc_tap.client import CCRSession, extract_repo, extract_text


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


class TestExtractRepo:
    def test_from_first_outcome(self):
        s = {"session_context": {"outcomes": [{"git_info": {"repo": "acme/widgets", "type": "github"}}]}}
        assert extract_repo(s) == "acme/widgets"

    def test_skips_outcomes_without_repo(self):
        s = {
            "session_context": {
                "outcomes": [
                    {"type": "other"},
                    {"git_info": {"branches": []}},
                    {"git_info": {"repo": "acme/widgets"}},
                ]
            }
        }
        assert extract_repo(s) == "acme/widgets"

    def test_empty_outcomes(self):
        assert extract_repo({"session_context": {"outcomes": []}}) == ""

    def test_missing_session_context(self):
        assert extract_repo({"id": "session_abc"}) == ""

    def test_null_session_context(self):
        assert extract_repo({"session_context": None}) == ""

    def test_non_dict_outcome_entries(self):
        assert extract_repo({"session_context": {"outcomes": ["nope", None]}}) == ""


class TestCCRSessionEnrichment:
    def test_enriched_fields_default_empty(self):
        s = CCRSession(id="abc", title="", status="", connection_status="", created_at="", updated_at="")
        assert (s.repo, s.status_bucket, s.status_detail) == ("", "", "")
