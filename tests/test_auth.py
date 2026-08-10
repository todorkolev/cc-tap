"""Tests for authentication helpers."""

import json
import os
from unittest.mock import patch

from cc_tap.auth import _extract_credentials, load_oauth_token


class TestLoadOAuthToken:
    def test_env_var(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}):
            result = load_oauth_token()
            assert result == {"accessToken": "test-token"}

    def test_no_credentials(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("cc_tap.auth.platform.system", return_value="Linux"),
            patch("cc_tap.auth.os.path.exists", return_value=False),
        ):
            result = load_oauth_token()
            assert result is None

    def test_plaintext_fallback(self, tmp_path):
        cred_file = tmp_path / ".credentials.json"
        cred_data = {"accessToken": "file-token", "refreshToken": "refresh"}
        cred_file.write_text(json.dumps(cred_data))

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("cc_tap.auth.platform.system", return_value="Linux"),
            patch("cc_tap.auth.os.path.expanduser", return_value=str(cred_file)),
            patch("cc_tap.auth.os.path.exists", return_value=True),
        ):
            result = load_oauth_token()
            assert result["accessToken"] == "file-token"


class TestCredentialShapes:
    """The CLI writes two different layouts; both must resolve."""

    def _write(self, tmp_path, payload):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / ".credentials.json").write_text(json.dumps(payload))
        return tmp_path

    def test_nested_claude_ai_oauth_from_file(self, tmp_path, monkeypatch):
        # What Claude Code actually writes on Linux.
        home = self._write(
            tmp_path,
            {"claudeAiOauth": {"accessToken": "nested-token", "expiresAt": 123}, "organizationUuid": "org"},
        )
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("cc_tap.auth.platform.system", lambda: "Linux")
        monkeypatch.setenv("HOME", str(home))
        result = load_oauth_token()
        assert result is not None
        assert result["accessToken"] == "nested-token"

    def test_flat_from_file(self, tmp_path, monkeypatch):
        home = self._write(tmp_path, {"accessToken": "flat-token"})
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("cc_tap.auth.platform.system", lambda: "Linux")
        monkeypatch.setenv("HOME", str(home))
        assert load_oauth_token()["accessToken"] == "flat-token"

    def test_file_without_token_is_none(self, tmp_path, monkeypatch):
        home = self._write(tmp_path, {"organizationUuid": "org-only"})
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("cc_tap.auth.platform.system", lambda: "Linux")
        monkeypatch.setenv("HOME", str(home))
        assert load_oauth_token() is None

    def test_reread_picks_up_refreshed_token(self, tmp_path, monkeypatch):
        """A refresh rewrites the file; the next call must see the new token."""
        home = self._write(tmp_path, {"claudeAiOauth": {"accessToken": "old"}})
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("cc_tap.auth.platform.system", lambda: "Linux")
        monkeypatch.setenv("HOME", str(home))
        assert load_oauth_token()["accessToken"] == "old"

        # Simulate the CLI's refresh: replace the file via atomic rename.
        target = home / ".claude" / ".credentials.json"
        tmp = home / ".claude" / ".credentials.json.new"
        tmp.write_text(json.dumps({"claudeAiOauth": {"accessToken": "refreshed"}}))
        os.replace(tmp, target)

        assert load_oauth_token()["accessToken"] == "refreshed"


class TestExtractCredentials:
    def test_nested_wins(self):
        data = {"claudeAiOauth": {"accessToken": "inner"}, "accessToken": "outer"}
        assert _extract_credentials(data)["accessToken"] == "inner"

    def test_ignores_empty_nested(self):
        assert _extract_credentials({"claudeAiOauth": {}, "accessToken": "outer"})["accessToken"] == "outer"

    def test_non_dict(self):
        assert _extract_credentials(["nope"]) is None
        assert _extract_credentials(None) is None
