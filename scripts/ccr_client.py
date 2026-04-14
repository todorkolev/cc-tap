"""
Claude Code Remote (CCR) Session Client

Proof-of-concept client for interacting with Claude Code Remote sessions.
Supports two auth modes:
  1. OAuth token (from Claude CLI login) — talks directly to api.anthropic.com
  2. Browser cookie — proxied through claude.ai

Usage:
    # Option A: Use your existing Claude CLI OAuth token (easiest)
    python ccr_client.py

    # Option B: Use browser cookie
    export CCR_COOKIE="your_cookie_string"
    export CCR_ORG_UUID="your_org_uuid"
    python ccr_client.py
"""

import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass

import requests

API_BASE_URL = "https://api.anthropic.com"
WEB_BASE_URL = "https://claude.ai"

CCR_BETA = "ccr-byoc-2025-07-29"


@dataclass
class CCRSession:
    id: str
    title: str
    status: str
    connection_status: str
    created_at: str
    updated_at: str

    @property
    def session_id(self) -> str:
        return self.id if self.id.startswith("session_") else f"session_{self.id}"

    @property
    def cse_id(self) -> str:
        raw = self.id.replace("session_", "").replace("cse_", "")
        return f"cse_{raw}"


def load_oauth_token() -> dict | None:
    """Load OAuth tokens from Claude CLI's credential store."""
    import platform
    import subprocess

    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return {"accessToken": os.environ["CLAUDE_CODE_OAUTH_TOKEN"]}

    if platform.system() == "Darwin":
        try:
            username = os.environ.get("USER", "claude-code-user")
            result = subprocess.run(
                ["security", "find-generic-password", "-a", username,
                 "-w", "-s", "Claude Code-credentials"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                raw = result.stdout.strip()
                data = json.loads(raw)
                if isinstance(data, dict):
                    if data.get("claudeAiOauth"):
                        return data["claudeAiOauth"]
                    if data.get("accessToken"):
                        return data
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            pass

    cred_path = os.path.expanduser("~/.claude/.credentials.json")
    if os.path.exists(cred_path):
        with open(cred_path) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("accessToken"):
            return data

    return None


def load_org_uuid(access_token: str = "") -> str | None:
    """Load org UUID from API or config files."""
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

    paths = [
        os.path.expanduser("~/.claude/local/.config.json"),
        os.path.expanduser("~/.claude/settings.json"),
    ]
    for path in paths:
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


class CCRClient:
    """Client for Claude Code Remote session API."""

    def __init__(self, access_token: str = "", org_uuid: str = "",
                 cookie: str = "", base_url: str = ""):
        self.org_uuid = org_uuid
        self.client_id = str(uuid.uuid4())
        self.http = requests.Session()

        if access_token:
            self.base_url = base_url or API_BASE_URL
            self.http.headers.update({
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": CCR_BETA,
            })
            self.access_token = access_token
        elif cookie:
            self.base_url = base_url or WEB_BASE_URL
            self.http.headers.update({
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": CCR_BETA,
                "anthropic-client-feature": "ccr",
                "cookie": cookie,
            })
            self.access_token = ""
        else:
            raise ValueError("Either access_token or cookie is required")

        if org_uuid:
            self.http.headers["x-organization-uuid"] = org_uuid

    def _sid(self, session_id: str) -> str:
        return session_id if session_id.startswith("session_") else f"session_{session_id}"

    def _cse(self, session_id: str) -> str:
        raw = session_id.replace("session_", "").replace("cse_", "")
        return f"cse_{raw}"

    def list_sessions(self) -> list[CCRSession]:
        resp = self.http.get(f"{self.base_url}/v1/sessions")
        resp.raise_for_status()
        return [
            CCRSession(
                id=s["id"],
                title=s.get("title") or "Untitled",
                status=s.get("session_status", "unknown"),
                connection_status=s.get("connection_status", "unknown"),
                created_at=s.get("created_at", ""),
                updated_at=s.get("updated_at", ""),
            )
            for s in resp.json().get("data", [])
        ]

    def get_session(self, session_id: str) -> dict:
        resp = self.http.get(f"{self.base_url}/v1/sessions/{self._sid(session_id)}")
        resp.raise_for_status()
        return resp.json()

    def get_events(self, session_id: str, limit: int = 1000) -> list[dict]:
        resp = self.http.get(
            f"{self.base_url}/v1/sessions/{self._sid(session_id)}/events",
            params={"limit": limit},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def send_presence(self, session_id: str) -> int:
        resp = self.http.post(
            f"{self.base_url}/v1/code/sessions/{self._cse(session_id)}/client/presence",
            json={"client_id": self.client_id},
        )
        resp.raise_for_status()
        return resp.json().get("refresh_after_seconds", 20)

    def send_message(self, session_id: str, message: str) -> dict:
        sid = self._sid(session_id)
        event = {
            "type": "user",
            "uuid": str(uuid.uuid4()),
            "session_id": sid,
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": message},
        }
        resp = self.http.post(
            f"{self.base_url}/v1/sessions/{sid}/events",
            json={"events": [event]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def post_control_response(self, session_id: str, request_id: str,
                               behavior: str = "allow",
                               updated_input: dict | None = None,
                               message: str = "") -> dict:
        sid = self._sid(session_id)
        response_inner = {"behavior": behavior}
        if behavior == "allow":
            response_inner["toolUseID"] = request_id
            if updated_input:
                response_inner["updatedInput"] = updated_input
        else:
            response_inner["message"] = message or "Denied by user"

        event = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response_inner,
            },
        }
        resp = self.http.post(
            f"{self.base_url}/v1/sessions/{sid}/events",
            json={"events": [event]},
        )
        resp.raise_for_status()
        return resp.json()


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


def print_event(ev: dict) -> None:
    """Print a single event to stdout."""
    etype = ev.get("type", "")

    if etype == "user":
        text = extract_text(ev.get("message", {}).get("content", ""))
        print(f"\n  [YOU] {text}")

    elif etype == "assistant":
        text = extract_text(ev.get("message", {}).get("content", []))
        if text:
            print(f"\n  [CC] {text}")

    elif etype == "control_request":
        req = ev.get("request", {})
        tool_name = req.get("tool_name", "?")
        desc = req.get("description", "")
        inp = req.get("input", {})
        print(f"\n  [TOOL REQUEST] {tool_name}: {desc}")
        if inp:
            inp_str = json.dumps(inp, indent=2)
            if len(inp_str) > 500:
                print(f"    {inp_str[:500]}...")
            else:
                print(f"    {inp_str}")
        print("    Use /approve or /deny")

    elif etype == "result":
        cost = ev.get("total_cost_usd", 0)
        if cost:
            print(f"\n  [DONE] ${cost:.4f}")


def print_conversation(events: list[dict], last_n: int = 10) -> None:
    """Print last N user/assistant exchanges."""
    conv = [e for e in events if e["type"] in ("user", "assistant", "result")]
    for ev in conv[-last_n:]:
        print_event(ev)


class BackgroundPoller:
    """Polls for new events in a background thread and prints them live."""

    def __init__(self, client: CCRClient, session_id: str, initial_count: int):
        self.client = client
        self.session_id = session_id
        self.event_count = initial_count
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        # Track UUIDs of messages we sent, so we don't echo them
        self._sent_uuids: set[str] = set()

    @property
    def count(self) -> int:
        with self._lock:
            return self.event_count

    @count.setter
    def count(self, value: int):
        with self._lock:
            self.event_count = value

    def mark_sent(self, msg_uuid: str):
        """Mark a message UUID as sent by us (to suppress echo)."""
        with self._lock:
            self._sent_uuids.add(msg_uuid)

    def start(self):
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def sync_count(self):
        """Refresh event count from server."""
        events = self.client.get_events(self.session_id)
        self.count = len(events)
        return events

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                events = self.client.get_events(self.session_id)
                current = len(events)

                with self._lock:
                    if current > self.event_count:
                        new_events = events[self.event_count:]
                        self.event_count = current

                        for ev in new_events:
                            # Skip user messages we sent ourselves
                            if ev.get("type") == "user":
                                ev_uuid = ev.get("uuid", "")
                                if ev_uuid in self._sent_uuids:
                                    self._sent_uuids.discard(ev_uuid)
                                    continue
                            print_event(ev)

                        # Redraw prompt after printing
                        sys.stdout.write("\n> ")
                        sys.stdout.flush()

                # Also send presence keepalive periodically
                self.client.send_presence(self.session_id)

            except Exception:
                pass  # Silently retry on network errors

            self._stop.wait(timeout=1.5)


def find_pending_requests(events: list[dict]) -> list[dict]:
    """Find control_requests that haven't been responded to."""
    responded_ids = set()
    for e in events:
        if e["type"] == "control_response":
            rid = e.get("response", {}).get("request_id", "")
            if rid:
                responded_ids.add(rid)

    pending = []
    for e in events:
        if e["type"] == "control_request":
            req = e.get("request", {})
            rid = req.get("tool_use_id") or e.get("request_id", "")
            if rid not in responded_ids:
                pending.append(e)
    return pending


def interactive_session(client: CCRClient, session: CCRSession) -> None:
    print(f"\nSession: {session.title}")
    print(f"ID: {session.id}")
    print("Commands: /quit /history /approve /deny /status /pause /resume")
    print("-" * 50)

    events = client.get_events(session.id)
    event_count = len(events)
    print_conversation(events, last_n=4)

    # Start background poller
    poller = BackgroundPoller(client, session.id, event_count)
    poller.start()
    print("\n  [live polling active]")

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            poller.stop()
            break

        if not user_input:
            continue

        if user_input == "/quit":
            poller.stop()
            break

        if user_input == "/history":
            events = poller.sync_count()
            print_conversation(events, last_n=10)
            continue

        if user_input == "/status":
            s = client.get_session(session.id)
            print(f"  Status: {s.get('session_status')} / {s.get('connection_status')}")
            print(f"  Events: {poller.count}")
            continue

        if user_input == "/approve":
            events = poller.sync_count()
            pending = find_pending_requests(events)
            if pending:
                req_event = pending[-1]
                inner = req_event.get("request", {})
                request_id = inner.get("tool_use_id") or req_event.get("request_id", "")
                tool_name = inner.get("tool_name", "?")
                print(f"  Approving: {tool_name}")
                client.post_control_response(
                    session.id, request_id, "allow",
                    updated_input=inner.get("input"),
                )
                print("  Approved.")
            else:
                print("  No pending tool requests.")
            continue

        if user_input == "/deny":
            events = poller.sync_count()
            pending = find_pending_requests(events)
            if pending:
                req_event = pending[-1]
                inner = req_event.get("request", {})
                request_id = inner.get("tool_use_id") or req_event.get("request_id", "")
                client.post_control_response(session.id, request_id, "deny")
                print("  Denied.")
            else:
                print("  No pending tool requests.")
            continue

        # Send message
        msg_uuid = str(uuid.uuid4())
        poller.mark_sent(msg_uuid)

        sid = client._sid(session.id)
        event = {
            "type": "user",
            "uuid": msg_uuid,
            "session_id": sid,
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": user_input},
        }
        try:
            resp = client.http.post(
                f"{client.base_url}/v1/sessions/{sid}/events",
                json={"events": [event]},
                timeout=30,
            )
            resp.raise_for_status()
            print("  Sent.")
        except Exception as e:
            print(f"  Send failed: {e}")

        # Response will appear via the background poller


def main():
    access_token = ""
    org_uuid = os.environ.get("CCR_ORG_UUID", "")
    cookie = os.environ.get("CCR_COOKIE", "")

    creds = load_oauth_token()
    if creds:
        access_token = creds.get("accessToken", "")
        if not org_uuid:
            org_uuid = (creds.get("organizationUuid", "")
                        or load_org_uuid(access_token) or "")

    if not access_token and not cookie:
        print("No credentials found.")
        print("")
        print("Option A: Run 'claude /login' first (OAuth token will be auto-detected)")
        print("")
        print("Option B: Set environment variables:")
        print('  export CCR_COOKIE="your_cookie"')
        print('  export CCR_ORG_UUID="your_org_uuid"')
        sys.exit(1)

    if access_token:
        print("Auth: OAuth token")
        client = CCRClient(access_token=access_token, org_uuid=org_uuid)
    else:
        print("Auth: Browser cookie")
        client = CCRClient(cookie=cookie, org_uuid=org_uuid)

    print("Claude Code Remote Session Client")
    print("=" * 50)

    print("\nFetching sessions...")
    try:
        sessions = client.list_sessions()
    except requests.HTTPError as e:
        print(f"Auth failed ({e.response.status_code}). Check credentials.")
        sys.exit(1)

    active = [s for s in sessions if s.status != "archived"]

    if not active:
        print("\nNo active sessions found.")
        return

    print(f"\nActive sessions ({len(active)}):")
    for i, s in enumerate(active[:20]):
        print(f"  {i:2d}. [{s.status:8s}] {s.title[:60]}")

    choice = input("\nSelect session (Enter for 0): ").strip()
    idx = int(choice) if choice.isdigit() else 0
    if idx >= len(active):
        print("Invalid.")
        return

    target = active[idx]
    client.send_presence(target.id)
    interactive_session(client, target)


if __name__ == "__main__":
    main()
