"""Tests for the CCR client."""

from cc_tap.client import CCRClient, CCRSession, extract_repo, extract_text


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


class TestGetEventsPagination:
    """The events endpoint caps limit at 1000 and returns oldest-first.

    Without paging, a session longer than that silently appears frozen at
    whenever its 1000th event happened.
    """

    def _client(self):
        return CCRClient(access_token="t", org_uuid="org")  # noqa: S106

    def _page(self, mocker_responses, ids, has_more, last_id=None):
        return {
            "data": [{"uuid": i, "type": "assistant"} for i in ids],
            "has_more": has_more,
            "last_id": last_id or (ids[-1] if ids else None),
        }

    def test_follows_has_more_across_pages(self, monkeypatch):
        c = self._client()
        pages = [
            self._page(None, [f"a{i}" for i in range(1000)], True),
            self._page(None, [f"b{i}" for i in range(1000)], True),
            self._page(None, [f"c{i}" for i in range(13)], False),
        ]
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params)
            return _FakeResp(pages[len(calls) - 1])

        monkeypatch.setattr(c.http, "get", fake_get)
        events = c.get_events("session_abc12345")

        assert len(events) == 2013
        assert events[-1]["uuid"] == "c12"
        assert calls[0].get("after_id") is None
        assert calls[1]["after_id"] == "a999"
        assert calls[2]["after_id"] == "b999"

    def test_stops_when_has_more_false(self, monkeypatch):
        c = self._client()
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params)
            return _FakeResp(self._page(None, ["x1", "x2"], False))

        monkeypatch.setattr(c.http, "get", fake_get)
        assert len(c.get_events("session_abc12345")) == 2
        assert len(calls) == 1

    def test_limit_clamped_to_server_maximum(self, monkeypatch):
        c = self._client()
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params)
            return _FakeResp(self._page(None, ["x"], False))

        monkeypatch.setattr(c.http, "get", fake_get)
        c.get_events("session_abc12345", limit=99999)
        assert calls[0]["limit"] == 1000  # asking for more is a 400

    def test_after_id_is_passed_through(self, monkeypatch):
        c = self._client()
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params)
            return _FakeResp(self._page(None, ["n1"], False))

        monkeypatch.setattr(c.http, "get", fake_get)
        c.get_events("session_abc12345", after_id="seen-this")
        assert calls[0]["after_id"] == "seen-this"

    def test_stalled_cursor_does_not_loop_forever(self, monkeypatch):
        """has_more stuck True with a non-advancing cursor must terminate."""
        c = self._client()
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params)
            return _FakeResp({"data": [{"uuid": "same"}], "has_more": True, "last_id": "same"})

        monkeypatch.setattr(c.http, "get", fake_get)
        events = c.get_events("session_abc12345", after_id="same")
        assert len(calls) == 1
        assert len(events) == 1

    def test_max_pages_bounds_the_walk(self, monkeypatch):
        c = self._client()
        n = {"i": 0}

        def fake_get(url, params=None, timeout=None):
            n["i"] += 1
            return _FakeResp(self._page(None, [f"p{n['i']}"], True))

        monkeypatch.setattr(c.http, "get", fake_get)
        c.get_events("session_abc12345", max_pages=3)
        assert n["i"] == 3

    def test_empty_history(self, monkeypatch):
        c = self._client()
        monkeypatch.setattr(
            c.http, "get", lambda *a, **k: _FakeResp({"data": [], "has_more": False})
        )
        assert c.get_events("session_abc12345") == []


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload
