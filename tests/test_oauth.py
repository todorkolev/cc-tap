"""Tests for the OAuth 2.1 authorization server."""

import stat
import time

import pytest
from mcp.server.auth.provider import AuthorizationParams

from cc_tap.oauth import (
    CCTapOAuthProvider,
    FileTokenStore,
    derive_client_credentials,
    is_allowed_redirect_uri,
)

SECRET = "s3cr3t-for-tests"  # noqa: S105
ISSUER = "https://cc-tap.example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def provider(tmp_path):
    return CCTapOAuthProvider(
        secret=SECRET, issuer_url=ISSUER, store=FileTokenStore(tmp_path / "tokens.json")
    )


def make_params(**overrides) -> AuthorizationParams:
    base = {
        "state": "st4te",
        "scopes": [],
        "code_challenge": "challenge-value",
        "redirect_uri": REDIRECT,
        "redirect_uri_provided_explicitly": True,
        "resource": None,
    }
    base.update(overrides)
    return AuthorizationParams(**base)


class TestDeriveClientCredentials:
    def test_deterministic(self):
        assert derive_client_credentials(SECRET) == derive_client_credentials(SECRET)

    def test_differs_per_secret(self):
        assert derive_client_credentials("a") != derive_client_credentials("b")

    def test_id_and_secret_differ(self):
        client_id, client_secret = derive_client_credentials(SECRET)
        assert client_id != client_secret
        assert len(client_id) == 36

    def test_secret_not_recoverable(self):
        client_id, client_secret = derive_client_credentials(SECRET)
        assert SECRET not in client_id
        assert SECRET not in client_secret


class TestRedirectUriAllowlist:
    @pytest.mark.parametrize(
        "uri",
        [
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "https://claude.ai/some/other/path",
        ],
    )
    def test_allowed(self, uri):
        assert is_allowed_redirect_uri(uri)

    @pytest.mark.parametrize(
        "uri",
        [
            "http://claude.ai/api/mcp/auth_callback",  # not https
            "https://evil.example.com/cb",
            "https://claude.ai.evil.com/cb",  # suffix trick
            "https://notclaude.ai/cb",
            "ftp://claude.ai/cb",
            "",
        ],
    )
    def test_rejected(self, uri):
        assert not is_allowed_redirect_uri(uri)


class TestFileTokenStore:
    def test_round_trip_across_instances(self, tmp_path):
        path = tmp_path / "t.json"
        FileTokenStore(path).put("access_tokens", "tok", {"token": "tok", "expires_at": None})
        assert FileTokenStore(path).get("access_tokens", "tok") == {"token": "tok", "expires_at": None}

    def test_file_is_owner_only(self, tmp_path):
        path = tmp_path / "t.json"
        FileTokenStore(path).put("access_tokens", "tok", {"expires_at": None})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_expired_entries_are_purged(self, tmp_path):
        path = tmp_path / "t.json"
        store = FileTokenStore(path)
        store.put("access_tokens", "old", {"expires_at": time.time() - 10})
        store.put("access_tokens", "new", {"expires_at": time.time() + 600})
        reloaded = FileTokenStore(path)
        assert reloaded.get("access_tokens", "old") is None
        assert reloaded.get("access_tokens", "new") is not None

    def test_corrupt_file_starts_fresh(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text("{not json")
        assert FileTokenStore(path).get("access_tokens", "anything") is None

    def test_pop_removes(self, tmp_path):
        store = FileTokenStore(tmp_path / "t.json")
        store.put("codes", "c", {"expires_at": None})
        assert store.pop("codes", "c") is not None
        assert store.pop("codes", "c") is None


class TestClients:
    async def test_fixed_client_resolves(self, provider):
        client = await provider.get_client(provider.client_id)
        assert client is not None
        assert client.client_secret == provider.client_secret

    async def test_unknown_client_is_none(self, provider):
        assert await provider.get_client("nope") is None

    async def test_register_then_resolve(self, provider):
        info = provider.fixed_client.model_copy(update={"client_id": "dyn-1"})
        await provider.register_client(info)
        assert (await provider.get_client("dyn-1")) is not None

    async def test_register_rejects_foreign_redirect(self, provider):
        info = provider.fixed_client.model_copy(
            update={"client_id": "dyn-2", "redirect_uris": ["https://evil.example.com/cb"]}
        )
        with pytest.raises(ValueError, match="not permitted"):
            await provider.register_client(info)

    async def test_registered_clients_survive_restart(self, provider, tmp_path):
        info = provider.fixed_client.model_copy(update={"client_id": "dyn-3"})
        await provider.register_client(info)
        revived = CCTapOAuthProvider(
            secret=SECRET, issuer_url=ISSUER, store=FileTokenStore(provider.store.path)
        )
        assert (await revived.get_client("dyn-3")) is not None


class TestAuthorizeConsent:
    async def test_authorize_goes_to_consent_not_callback(self, provider):
        target = await provider.authorize(provider.fixed_client, make_params())
        assert target.startswith(f"{ISSUER}/consent?rid=")
        assert "code=" not in target

    async def test_authorize_rejects_foreign_redirect(self, provider):
        params = make_params(redirect_uri="https://evil.example.com/cb")
        with pytest.raises(ValueError, match="not permitted"):
            await provider.authorize(provider.fixed_client, params)

    async def test_wrong_secret_issues_nothing(self, provider):
        target = await provider.authorize(provider.fixed_client, make_params())
        rid = target.split("rid=")[1]
        assert provider.approve_pending(rid, "wrong") is None
        # the request stays pending so the user can retry
        assert provider.pending_exists(rid)

    async def test_correct_secret_redirects_with_code_and_state(self, provider):
        target = await provider.authorize(provider.fixed_client, make_params())
        rid = target.split("rid=")[1]
        redirect = provider.approve_pending(rid, SECRET)
        assert redirect.startswith(REDIRECT)
        assert "code=" in redirect
        assert "state=st4te" in redirect
        assert not provider.pending_exists(rid)

    async def test_auto_approve_skips_consent(self, tmp_path):
        provider = CCTapOAuthProvider(
            secret=SECRET,
            issuer_url=ISSUER,
            store=FileTokenStore(tmp_path / "t.json"),
            auto_approve=True,
        )
        target = await provider.authorize(provider.fixed_client, make_params())
        assert target.startswith(REDIRECT)
        assert "code=" in target

    async def test_consent_page_escapes_rid(self, provider):
        page = provider.render_consent_page('"><script>x</script>')
        assert "<script>x</script>" not in page
        assert "&lt;script&gt;" in page


class TestTokenExchange:
    async def _code_for(self, provider) -> str:
        target = await provider.authorize(provider.fixed_client, make_params())
        redirect = provider.approve_pending(target.split("rid=")[1], SECRET)
        return redirect.split("code=")[1].split("&")[0]

    async def test_code_carries_pkce_challenge(self, provider):
        code = await self._code_for(provider)
        loaded = await provider.load_authorization_code(provider.fixed_client, code)
        assert loaded.code_challenge == "challenge-value"

    async def test_code_is_single_use(self, provider):
        code = await self._code_for(provider)
        loaded = await provider.load_authorization_code(provider.fixed_client, code)
        await provider.exchange_authorization_code(provider.fixed_client, loaded)
        with pytest.raises(ValueError, match="already used"):
            await provider.exchange_authorization_code(provider.fixed_client, loaded)

    async def test_code_not_loadable_by_other_client(self, provider):
        code = await self._code_for(provider)
        other = provider.fixed_client.model_copy(update={"client_id": "someone-else"})
        assert await provider.load_authorization_code(other, code) is None

    async def test_expired_code_rejected(self, provider):
        code = await self._code_for(provider)
        raw = provider.store.get("codes", code)
        raw["expires_at"] = time.time() - 1
        provider.store.put("codes", code, raw)
        assert await provider.load_authorization_code(provider.fixed_client, code) is None

    async def test_access_token_validates(self, provider):
        code = await self._code_for(provider)
        loaded = await provider.load_authorization_code(provider.fixed_client, code)
        token = await provider.exchange_authorization_code(provider.fixed_client, loaded)
        access = await provider.load_access_token(token.access_token)
        assert access is not None
        assert access.client_id == provider.client_id

    async def test_refresh_rotates(self, provider):
        code = await self._code_for(provider)
        loaded = await provider.load_authorization_code(provider.fixed_client, code)
        token = await provider.exchange_authorization_code(provider.fixed_client, loaded)

        refresh = await provider.load_refresh_token(provider.fixed_client, token.refresh_token)
        new_token = await provider.exchange_refresh_token(provider.fixed_client, refresh, [])
        assert new_token.access_token != token.access_token

        with pytest.raises(ValueError, match="already used"):
            await provider.exchange_refresh_token(provider.fixed_client, refresh, [])

    async def test_expired_access_token_rejected(self, provider):
        code = await self._code_for(provider)
        loaded = await provider.load_authorization_code(provider.fixed_client, code)
        token = await provider.exchange_authorization_code(provider.fixed_client, loaded)
        raw = provider.store.get("access_tokens", token.access_token)
        raw["expires_at"] = int(time.time()) - 1
        provider.store.put("access_tokens", token.access_token, raw)
        assert await provider.load_access_token(token.access_token) is None

    async def test_tokens_survive_restart(self, provider):
        code = await self._code_for(provider)
        loaded = await provider.load_authorization_code(provider.fixed_client, code)
        token = await provider.exchange_authorization_code(provider.fixed_client, loaded)

        revived = CCTapOAuthProvider(
            secret=SECRET, issuer_url=ISSUER, store=FileTokenStore(provider.store.path)
        )
        assert await revived.load_access_token(token.access_token) is not None

    async def test_revoke(self, provider):
        code = await self._code_for(provider)
        loaded = await provider.load_authorization_code(provider.fixed_client, code)
        token = await provider.exchange_authorization_code(provider.fixed_client, loaded)
        access = await provider.load_access_token(token.access_token)
        await provider.revoke_token(access)
        assert await provider.load_access_token(token.access_token) is None

    async def test_store_holds_no_plaintext_secret(self, provider):
        await self._code_for(provider)
        assert SECRET not in provider.store.path.read_text()
