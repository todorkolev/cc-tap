"""Streamable-HTTP transport for cc-tap, with OAuth 2.1 in front of ``/mcp``.

Layout of the served app:

===============================================  ====================================
``/mcp``                                          MCP streamable HTTP — auth required
``/health``                                       liveness probe — no auth
``/consent``                                      approval form — asks for MCP_SECRET
``/authorize``, ``/token``, ``/register``          OAuth endpoints (from the MCP SDK)
``/.well-known/oauth-authorization-server``        AS metadata (RFC 8414)
``/.well-known/oauth-protected-resource/mcp``      RS metadata (RFC 9728)
===============================================  ====================================
"""

from __future__ import annotations

import logging
import os
from urllib.parse import parse_qs

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from cc_tap import __version__
from cc_tap.auth import load_oauth_token
from cc_tap.oauth import CONSENT_PATH, CCTapOAuthProvider, FileTokenStore
from cc_tap.server import build_mcp

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_STORE = "~/.cc-tap/oauth-tokens.json"  # noqa: S105 — a path, not a secret


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_http_app(*, host: str, port: int, public_url: str | None):
    """Build the FastMCP instance for HTTP, with OAuth and extra routes attached."""
    secret = os.environ.get("MCP_SECRET", "").strip()
    if not secret:
        msg = (
            "MCP_SECRET is required for --transport http (it gates the OAuth consent "
            "step and derives the pre-registered client credentials). "
            "Generate one with: openssl rand -hex 32"
        )
        raise SystemExit(msg)

    # The issuer must be the URL clients actually reach — behind a TLS proxy that
    # is not the bind address, so it cannot be inferred from host/port.
    issuer = (public_url or f"http://{host}:{port}").rstrip("/")
    if not public_url:
        logger.warning(
            "CC_TAP_PUBLIC_URL is not set; using %s as the OAuth issuer. "
            "Behind a TLS proxy this must be the public HTTPS URL or the flow will fail.",
            issuer,
        )

    store_path = os.path.expanduser(os.environ.get("CC_TAP_TOKEN_STORE", DEFAULT_TOKEN_STORE))
    try:
        provider = CCTapOAuthProvider(
            secret=secret,
            issuer_url=issuer,
            store=FileTokenStore(store_path),
            auto_approve=_env_flag("CC_TAP_OAUTH_AUTO_APPROVE", False),
            static_token=os.environ.get("CC_TAP_BEARER_TOKEN", "").strip(),
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e

    auth_settings = AuthSettings(
        issuer_url=issuer,  # type: ignore[arg-type]
        resource_server_url=f"{issuer}/mcp",  # type: ignore[arg-type]
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )

    server = build_mcp(
        auth_server_provider=provider,
        auth=auth_settings,
        host=host,
        port=port,
        # Stateless avoids per-session state that would otherwise have to survive
        # restarts and stick to one instance behind a proxy. All six tools are
        # plain request/response, so nothing here needs a long-lived stream.
        stateless_http=_env_flag("CC_TAP_STATELESS_HTTP", True),
    )

    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> Response:
        # Unauthenticated on purpose: custom routes sit outside the auth
        # middleware, which only wraps the MCP endpoint. Reports whether Claude
        # Code credentials are readable, but never any part of the token.
        creds = "present" if load_oauth_token() else "missing"
        return JSONResponse(
            {"status": "ok", "service": "cc-tap", "version": __version__, "credentials": creds}
        )

    @server.custom_route(CONSENT_PATH, methods=["GET"])
    async def consent_form(request: Request) -> Response:
        rid = request.query_params.get("rid", "")
        if not rid or not provider.pending_exists(rid):
            return HTMLResponse(
                "<h1>Link expired</h1><p>Start the connection again from Claude.</p>", status_code=400
            )
        return HTMLResponse(provider.render_consent_page(rid))

    @server.custom_route(CONSENT_PATH, methods=["POST"])
    async def consent_submit(request: Request) -> Response:
        # Parsed by hand so the app does not need python-multipart just to read
        # two urlencoded fields.
        body = (await request.body()).decode("utf-8", "replace")
        form = parse_qs(body)
        rid = (form.get("rid") or [""])[0]
        attempt = (form.get("secret") or [""])[0]

        if not rid or not provider.pending_exists(rid):
            return HTMLResponse(
                "<h1>Link expired</h1><p>Start the connection again from Claude.</p>", status_code=400
            )

        target = provider.approve_pending(rid, attempt)
        if target is None:
            # Deliberately vague: do not distinguish a wrong secret from an
            # expired request.
            return HTMLResponse(
                provider.render_consent_page(rid, error="Incorrect secret — try again."),
                status_code=401,
            )
        return RedirectResponse(target, status_code=302)

    return server, provider, issuer


def run_http(*, host: str, port: int, public_url: str | None) -> None:
    """Build and serve the HTTP app."""
    server, provider, issuer = build_http_app(host=host, port=port, public_url=public_url)

    logger.info("cc-tap %s serving MCP over streamable HTTP", __version__)
    logger.info("  bind:          %s:%s", host, port)
    logger.info("  public URL:    %s", issuer)
    logger.info("  MCP endpoint:  %s/mcp", issuer)
    logger.info("  health:        %s/health", issuer)
    logger.info("")
    logger.info("Add in Claude.ai -> Settings -> Connectors -> Add custom connector:")
    logger.info("  URL:            %s/mcp", issuer)
    logger.info("  Leave Client ID / Client Secret blank to use dynamic registration, or paste:")
    logger.info("  Client ID:      %s", provider.client_id)
    logger.info("  Client Secret:  %s", provider.client_secret)
    logger.info("")
    logger.info("Approving the connection requires MCP_SECRET on the consent page.")
    if provider.static_token:
        logger.info(
            "A pre-shared bearer token is also accepted (CC_TAP_BEARER_TOKEN, %d chars). "
            "For clients that offer a bearer field but do not run the OAuth flow.",
            len(provider.static_token),
        )
    if provider.auto_approve:
        logger.warning("CC_TAP_OAUTH_AUTO_APPROVE is on — anyone who can reach this URL can connect.")

    server.run(transport="streamable-http")
