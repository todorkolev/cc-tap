"""Authentication helpers for Claude Code OAuth tokens.

Reads credentials from:
  1. CLAUDE_CODE_OAUTH_TOKEN env var
  2. macOS Keychain (service: 'Claude Code-credentials')
  3. ~/.claude/.credentials.json (plaintext fallback)

Reads org UUID from:
  1. /api/oauth/claude_cli/roles endpoint
  2. Local config files
"""

import json
import os
import platform
import subprocess

import requests

API_BASE_URL = "https://api.anthropic.com"


def _extract_credentials(data) -> dict | None:
    """Normalise a credential payload to the dict holding ``accessToken``.

    The CLI stores credentials in two shapes, and both turn up in both the
    Keychain and the plaintext file depending on version and platform:

      {"claudeAiOauth": {"accessToken": ...}, "organizationUuid": ...}   nested
      {"accessToken": ...}                                              flat
    """
    if not isinstance(data, dict):
        return None
    nested = data.get("claudeAiOauth")
    if isinstance(nested, dict) and nested.get("accessToken"):
        return nested
    if data.get("accessToken"):
        return data
    return None


def load_oauth_token() -> dict | None:
    """Load OAuth tokens from Claude CLI's credential store."""
    # 1. Environment variable
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return {"accessToken": os.environ["CLAUDE_CODE_OAUTH_TOKEN"]}

    # 2. macOS Keychain
    if platform.system() == "Darwin":
        try:
            username = os.environ.get("USER", "claude-code-user")
            result = subprocess.run(
                ["security", "find-generic-password", "-a", username, "-w", "-s", "Claude Code-credentials"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                creds = _extract_credentials(json.loads(result.stdout.strip()))
                if creds:
                    return creds
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            pass

    # 3. Plaintext fallback. Read fresh every call and never cached here: the
    # token expires and is rewritten by the running Claude Code CLI, so callers
    # re-invoke this on 401 to pick up the refreshed value.
    cred_path = os.path.expanduser("~/.claude/.credentials.json")
    try:
        if os.path.exists(cred_path):
            with open(cred_path) as f:
                return _extract_credentials(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass

    return None


def load_org_uuid(access_token: str = "") -> str | None:
    """Load organization UUID from env var, API, or local config."""
    # Check env var first
    env_org = os.environ.get("CCR_ORG_UUID")
    if env_org:
        return env_org

    if access_token:
        try:
            resp = requests.get(
                f"{API_BASE_URL}/api/oauth/claude_cli/roles",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "anthropic-version": "2023-06-01",
                },
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                if isinstance(data, dict) and data.get("organization_uuid"):
                    return data["organization_uuid"]
        except requests.RequestException:
            pass

    for path in [
        os.path.expanduser("~/.claude/local/.config.json"),
        os.path.expanduser("~/.claude/settings.json"),
    ]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    org = data.get("organizationUuid") or data.get("org_uuid")
                    if org:
                        return org
            except (json.JSONDecodeError, KeyError):
                continue
    return None
