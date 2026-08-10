"""OAuth 2.1 authorization server for cc-tap's HTTP transport.

Implements the provider interface the MCP Python SDK expects
(``mcp.server.auth.provider.OAuthAuthorizationServerProvider``), backed by a
JSON file so issued tokens survive a restart.

There are two ways to connect a client, and both work:

* **Dynamic Client Registration** — Claude registers itself and the two
  credential fields in the "Add custom connector" form stay blank.
* **Pre-registered credentials** — a fixed client ID and secret derived from
  ``MCP_SECRET`` and printed at startup, pasted into those two fields.

Either way the browser lands on a consent page that requires ``MCP_SECRET``
before an authorization code is issued. That consent step *is* the access
gate. With DCR enabled anyone who can reach the server can register a client,
so approving automatically would leave the endpoint effectively open to
whoever knows the URL.

The overall approach — SHA-256-derived fixed credentials, file-persisted
tokens, redirect URIs restricted to claude.ai / claude.com — follows
https://github.com/afonsofigs/claude-code-rc-mcp (MIT, (c) Afonso Figuinha).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlsplit, urlunsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

#: Only Anthropic's connector callback hosts may receive authorization codes.
ALLOWED_REDIRECT_HOSTS = ("claude.ai", "claude.com")

DEFAULT_TOKEN_TTL = 86400  # 24h
AUTH_CODE_TTL = 300  # 5min, per OAuth 2.1 guidance
PENDING_TTL = 600  # how long a consent page stays valid

CONSENT_PATH = "/consent"


def derive_client_credentials(secret: str) -> tuple[str, str]:
    """Derive a stable client ID and secret from ``MCP_SECRET``.

    Deterministic so the values can be pasted into Claude's connector form
    once and keep working across restarts and redeploys.
    """
    client_id = hashlib.sha256(f"{secret}:client_id".encode()).hexdigest()[:36]
    client_secret = hashlib.sha256(f"{secret}:client_secret".encode()).hexdigest()
    return client_id, client_secret


def is_allowed_redirect_uri(uri: str) -> bool:
    """True if ``uri`` points at an Anthropic connector callback over HTTPS."""
    try:
        parsed = urlparse(str(uri))
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in ALLOWED_REDIRECT_HOSTS


class FileTokenStore:
    """JSON-file-backed store for clients, codes and tokens.

    Small enough that rewriting the whole file per mutation is fine. Writes go
    through a temp file plus ``os.replace`` so a crash mid-write cannot leave a
    truncated store behind, and the file is created 0600 because it holds
    bearer tokens.
    """

    _SECTIONS = ("clients", "codes", "access_tokens", "refresh_tokens", "pending")

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.data: dict[str, dict[str, Any]] = {s: {} for s in self._SECTIONS}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict):
                    for section in self._SECTIONS:
                        got = loaded.get(section)
                        if isinstance(got, dict):
                            self.data[section] = got
                    logger.info("Loaded OAuth store from %s", self.path)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not read OAuth store %s (%s); starting fresh", self.path, e)

    def save(self) -> None:
        self._purge_expired()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as e:
            # A store we cannot persist still works in memory until restart —
            # worth a loud warning, not a crash that takes the server down.
            logger.error("Could not persist OAuth store %s: %s", self.path, e)

    def _purge_expired(self) -> None:
        now = time.time()
        for section, field in (
            ("codes", "expires_at"),
            ("access_tokens", "expires_at"),
            ("refresh_tokens", "expires_at"),
            ("pending", "expires_at"),
        ):
            live = {}
            for key, value in self.data[section].items():
                expires_at = value.get(field)
                if expires_at is None or expires_at > now:
                    live[key] = value
            self.data[section] = live

    def get(self, section: str, key: str) -> dict[str, Any] | None:
        return self.data[section].get(key)

    def put(self, section: str, key: str, value: dict[str, Any]) -> None:
        self.data[section][key] = value
        self.save()

    def pop(self, section: str, key: str) -> dict[str, Any] | None:
        value = self.data[section].pop(key, None)
        if value is not None:
            self.save()
        return value


class CCTapOAuthProvider(OAuthAuthorizationServerProvider):
    """OAuth 2.1 provider gating the ``/mcp`` endpoint."""

    def __init__(
        self,
        *,
        secret: str,
        issuer_url: str,
        store: FileTokenStore,
        auto_approve: bool = False,
        token_ttl: int = DEFAULT_TOKEN_TTL,
    ):
        self.secret = secret
        self.issuer_url = str(issuer_url).rstrip("/")
        self.store = store
        self.auto_approve = auto_approve
        self.token_ttl = token_ttl
        self.client_id, self.client_secret = derive_client_credentials(secret)

    # --- clients ---------------------------------------------------------

    @property
    def fixed_client(self) -> OAuthClientInformationFull:
        """The pre-registered client, for pasting into Claude's form."""
        return OAuthClientInformationFull(
            client_id=self.client_id,
            client_secret=self.client_secret,
            client_name="cc-tap (pre-registered)",
            redirect_uris=[
                "https://claude.ai/api/mcp/auth_callback",  # type: ignore[list-item]
                "https://claude.com/api/mcp/auth_callback",  # type: ignore[list-item]
            ],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post",  # noqa: S106
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if hmac.compare_digest(client_id, self.client_id):
            return self.fixed_client
        raw = self.store.get("clients", client_id)
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except ValueError:
            logger.warning("Discarding unparseable stored client %s", client_id)
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Persist a dynamically registered client.

        Registration is open, but useless on its own: an authorization code is
        only issued after the consent page accepts ``MCP_SECRET``.
        """
        bad = [str(u) for u in client_info.redirect_uris or [] if not is_allowed_redirect_uri(str(u))]
        if bad:
            hosts = " or ".join(ALLOWED_REDIRECT_HOSTS)
            msg = f"redirect_uri not permitted: {bad[0]} (must be https on {hosts})"
            raise ValueError(msg)
        self.store.put("clients", client_info.client_id, client_info.model_dump(mode="json"))
        logger.info("Registered client %s (%s)", client_info.client_id, client_info.client_name)

    # --- authorization ---------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        redirect_uri = str(params.redirect_uri)
        if not is_allowed_redirect_uri(redirect_uri):
            msg = f"redirect_uri not permitted: {redirect_uri}"
            raise ValueError(msg)

        if self.auto_approve:
            logger.warning("CC_TAP_OAUTH_AUTO_APPROVE is set — issuing a code without consent")
            return self._issue_code_redirect(client.client_id, params)

        rid = secrets.token_urlsafe(24)
        self.store.put(
            "pending",
            rid,
            {
                "client_id": client.client_id,
                "params": self._dump_params(params),
                "expires_at": time.time() + PENDING_TTL,
            },
        )
        logger.info("Authorization request %s from client %s awaiting consent", rid[:8], client.client_id)
        return f"{self.issuer_url}{CONSENT_PATH}?{urlencode({'rid': rid})}"

    def approve_pending(self, rid: str, secret_attempt: str) -> str | None:
        """Validate the consent form and return the redirect URL, or None.

        None means the secret was wrong or the request expired — the caller
        re-renders the form rather than leaking which of the two it was.
        """
        pending = self.store.get("pending", rid)
        if not pending:
            return None
        if not hmac.compare_digest(secret_attempt, self.secret):
            logger.warning("Rejected consent for request %s: wrong secret", rid[:8])
            return None
        self.store.pop("pending", rid)
        params = self._load_params(pending["params"])
        logger.info("Approved authorization request %s", rid[:8])
        return self._issue_code_redirect(pending["client_id"], params)

    def pending_exists(self, rid: str) -> bool:
        return self.store.get("pending", rid) is not None

    def _issue_code_redirect(self, client_id: str, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(32)
        self.store.put(
            "codes",
            code,
            {
                "code": code,
                "client_id": client_id,
                "scopes": params.scopes or [],
                "expires_at": time.time() + AUTH_CODE_TTL,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
            },
        )
        query = {"code": code}
        if params.state:
            query["state"] = params.state
        parts = urlsplit(str(params.redirect_uri))
        # Preserve any query already on the callback rather than clobbering it.
        merged = f"{parts.query}&{urlencode(query)}" if parts.query else urlencode(query)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, merged, parts.fragment))

    @staticmethod
    def _dump_params(params: AuthorizationParams) -> dict[str, Any]:
        return {
            "state": params.state,
            "scopes": params.scopes,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        }

    @staticmethod
    def _load_params(raw: dict[str, Any]) -> AuthorizationParams:
        return AuthorizationParams(
            state=raw.get("state"),
            scopes=raw.get("scopes"),
            code_challenge=raw["code_challenge"],
            redirect_uri=raw["redirect_uri"],  # type: ignore[arg-type]
            redirect_uri_provided_explicitly=raw.get("redirect_uri_provided_explicitly", True),
            resource=raw.get("resource"),
        )

    # --- code / token exchange -------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        raw = self.store.get("codes", authorization_code)
        if not raw or raw["client_id"] != client.client_id:
            return None
        if raw["expires_at"] <= time.time():
            self.store.pop("codes", authorization_code)
            return None
        return AuthorizationCode.model_validate(raw)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: drop the code before minting anything against it.
        if self.store.pop("codes", authorization_code.code) is None:
            msg = "Authorization code already used or expired"
            raise ValueError(msg)
        return self._issue_tokens(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        raw = self.store.get("refresh_tokens", refresh_token)
        if not raw or raw["client_id"] != client.client_id:
            return None
        return RefreshToken.model_validate(raw)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the presented refresh token is spent even if issuing fails.
        if self.store.pop("refresh_tokens", refresh_token.token) is None:
            msg = "Refresh token already used or expired"
            raise ValueError(msg)
        return self._issue_tokens(client.client_id, scopes or refresh_token.scopes, None)

    def _issue_tokens(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + self.token_ttl
        self.store.put(
            "access_tokens",
            access_token,
            {
                "token": access_token,
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": expires_at,
                "resource": resource,
            },
        )
        self.store.put(
            "refresh_tokens",
            refresh_token,
            {"token": refresh_token, "client_id": client_id, "scopes": scopes, "expires_at": None},
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",  # noqa: S106
            expires_in=self.token_ttl,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = self.store.get("access_tokens", token)
        if not raw:
            return None
        if raw["expires_at"] is not None and raw["expires_at"] <= time.time():
            self.store.pop("access_tokens", token)
            return None
        return AccessToken.model_validate(raw)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        section = "access_tokens" if isinstance(token, AccessToken) else "refresh_tokens"
        self.store.pop(section, token.token)

    # --- consent page -----------------------------------------------------

    def render_consent_page(self, rid: str, *, error: str = "") -> str:
        """Minimal self-contained consent form (no external assets)."""
        banner = f'<p class="err">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize cc-tap</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; margin: 0;
         min-height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText; }}
  .card {{ width: min(92vw, 26rem); padding: 2rem; border-radius: 12px;
           border: 1px solid color-mix(in srgb, CanvasText 18%, transparent); }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .5rem; }}
  p {{ margin: 0 0 1.25rem; line-height: 1.5; opacity: .8; font-size: .9rem; }}
  .err {{ color: #b3261e; opacity: 1; }}
  label {{ display: block; font-size: .8rem; font-weight: 600; margin-bottom: .35rem; }}
  input {{ width: 100%; box-sizing: border-box; padding: .6rem .7rem; font-size: 1rem; border-radius: 8px;
           border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
           background: Canvas; color: CanvasText; }}
  button {{ margin-top: 1rem; width: 100%; padding: .65rem; font-size: .95rem; font-weight: 600;
            border: 0; border-radius: 8px; background: #c96442; color: #fff; cursor: pointer; }}
</style>
</head>
<body>
  <main class="card">
    <h1>Authorize cc-tap</h1>
    <p>Claude is asking to connect to this cc-tap server. Enter the server secret to approve.</p>
    {banner}
    <form method="post" action="{CONSENT_PATH}">
      <input type="hidden" name="rid" value="{html.escape(rid)}">
      <label for="secret">Server secret</label>
      <input id="secret" name="secret" type="password" autocomplete="off" autofocus required>
      <button type="submit">Approve</button>
    </form>
  </main>
</body>
</html>"""
