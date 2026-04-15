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
                data = json.loads(result.stdout.strip())
                if isinstance(data, dict):
                    if data.get("claudeAiOauth"):
                        return data["claudeAiOauth"]
                    if data.get("accessToken"):
                        return data
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            pass

    # 3. Plaintext fallback
    cred_path = os.path.expanduser("~/.claude/.credentials.json")
    try:
        if os.path.exists(cred_path):
            with open(cred_path) as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("accessToken"):
                return data
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
