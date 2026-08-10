"""HTTP client for the Claude Code Remote session API."""

import re
import uuid
from dataclasses import dataclass

import requests

API_BASE_URL = "https://api.anthropic.com"
CCR_BETA = "ccr-byoc-2025-07-29"

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")


@dataclass
class CCRSession:
    """A Claude Code Remote session."""

    id: str
    title: str
    status: str
    connection_status: str
    created_at: str
    updated_at: str
    #: Enriched fields the list endpoint already returns, so callers do not have
    #: to fetch each session individually just to learn its repo or progress.
    repo: str = ""
    status_bucket: str = ""
    status_detail: str = ""

    @property
    def session_id(self) -> str:
        return self.id if self.id.startswith("session_") else f"session_{self.id}"

    @property
    def cse_id(self) -> str:
        raw = self.id.replace("session_", "").replace("cse_", "")
        return f"cse_{raw}"


class CCRClient:
    """Client for the Claude Code Remote session API.

    Talks to api.anthropic.com using OAuth Bearer tokens.
    """

    def __init__(self, access_token: str, org_uuid: str, timeout: tuple[int, int] = (5, 30)):
        self.org_uuid = org_uuid
        self.client_id = str(uuid.uuid4())
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": CCR_BETA,
                "x-organization-uuid": org_uuid,
            }
        )

    def _validate_id(self, session_id: str) -> str:
        """Strip known prefixes and validate the raw ID format."""
        raw = session_id.replace("session_", "").replace("cse_", "")
        if not _SESSION_ID_RE.match(raw):
            msg = f"Invalid session_id: {session_id!r}"
            raise ValueError(msg)
        return raw

    def _sid(self, session_id: str) -> str:
        return f"session_{self._validate_id(session_id)}"

    def _cse(self, session_id: str) -> str:
        return f"cse_{self._validate_id(session_id)}"

    def list_sessions(self) -> list[CCRSession]:
        """List all remote sessions."""
        resp = self.http.get(f"{API_BASE_URL}/v1/sessions", timeout=self.timeout)
        resp.raise_for_status()
        return [
            CCRSession(
                id=s["id"],
                title=s.get("title") or "Untitled",
                status=s.get("session_status", "unknown"),
                connection_status=s.get("connection_status", "unknown"),
                created_at=s.get("created_at", ""),
                updated_at=s.get("updated_at", ""),
                repo=extract_repo(s),
                status_bucket=s.get("status_bucket") or "",
                status_detail=(s.get("post_turn_summary") or {}).get("status_detail") or "",
            )
            for s in resp.json().get("data", [])
        ]

    def get_session(self, session_id: str) -> dict:
        """Get a single session's details."""
        resp = self.http.get(f"{API_BASE_URL}/v1/sessions/{self._sid(session_id)}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_events(self, session_id: str, limit: int = 1000) -> list[dict]:
        """Get conversation history for a session."""
        resp = self.http.get(
            f"{API_BASE_URL}/v1/sessions/{self._sid(session_id)}/events",
            params={"limit": limit},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def send_presence(self, session_id: str) -> int:
        """Send client presence heartbeat. Returns seconds until next heartbeat."""
        resp = self.http.post(
            f"{API_BASE_URL}/v1/code/sessions/{self._cse(session_id)}/client/presence",
            json={"client_id": self.client_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("refresh_after_seconds", 20)

    def send_message(self, session_id: str, message: str) -> dict:
        """Send a user message to a session."""
        sid = self._sid(session_id)
        event = {
            "type": "user",
            "uuid": str(uuid.uuid4()),
            "session_id": sid,
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": message},
        }
        resp = self.http.post(
            f"{API_BASE_URL}/v1/sessions/{sid}/events",
            json={"events": [event]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


def extract_repo(session: dict) -> str:
    """Pull the repo slug out of a raw session dict.

    Lives at ``session_context.outcomes[].git_info.repo``. Outcomes is a list
    and some sessions have none (a session started outside a repo), so take the
    first entry that actually names one.
    """
    outcomes = (session.get("session_context") or {}).get("outcomes") or []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        repo = (outcome.get("git_info") or {}).get("repo")
        if repo:
            return str(repo)
    return ""


def extract_text(content) -> str:
    """Extract all text from a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""
