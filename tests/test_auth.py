"""Tests for authentication helpers."""

import json
import os
from unittest.mock import patch

from cc_tap.auth import load_oauth_token


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
