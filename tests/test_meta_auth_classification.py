"""Meta (Threads + Instagram) auth-error classification in validate_session().

Regression cover for the 2.83.0 fix: a Meta OAuthException code **200**
("API access blocked" — an app-level block on the user's Meta app) must NOT be
reported as an expired credential. Only code **190** (a genuinely expired /
invalid token) may return ``None`` (which the session checker renders as the
red "expired — re-enter credentials"); every other auth failure raises so the
checker renders an amber "couldn't verify" with the real reason instead.
"""

import pytest
import respx
import httpx

from clients.thr.client import ThrClient, ThrAuthError
from clients.ig.client import IgClient, IgAuthError
from clients.thr import client as thr_mod
from clients.ig import client as ig_mod


# ── Threads ──────────────────────────────────────────────────

class TestThrAuthClassification:

    def _mock_refresh(self):
        # _try_refresh() is best-effort; give it a benign 400 so it no-ops.
        respx.get(thr_mod._REFRESH_URL).mock(return_value=httpx.Response(400, json={}))

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_access_blocked_raises_not_expired(self):
        self._mock_refresh()
        respx.get(f"{thr_mod._API_BASE}/me").mock(return_value=httpx.Response(
            400, json={"error": {"message": "API access blocked.",
                                 "type": "OAuthException", "code": 200}}))
        c = ThrClient(access_token="tok")
        with pytest.raises(ThrAuthError):
            await c.validate_session()
        await c.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_expired_token_code_190_returns_none(self):
        self._mock_refresh()
        respx.get(f"{thr_mod._API_BASE}/me").mock(return_value=httpx.Response(
            400, json={"error": {"message": "Error validating access token: expired",
                                 "type": "OAuthException", "code": 190}}))
        c = ThrClient(access_token="tok")
        assert await c.validate_session() is None      # → 'expired' (correct)
        await c.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_valid_token_returns_username(self):
        self._mock_refresh()
        respx.get(f"{thr_mod._API_BASE}/me").mock(return_value=httpx.Response(
            200, json={"id": "12345", "username": "kithe"}))
        c = ThrClient(access_token="tok")
        assert await c.validate_session() == "kithe"
        await c.close()


# ── Instagram ────────────────────────────────────────────────

class TestIgAuthClassification:

    def _mock_refresh(self):
        respx.get(ig_mod._REFRESH_URL).mock(return_value=httpx.Response(400, json={}))

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_access_blocked_raises_not_expired(self):
        self._mock_refresh()
        respx.get(f"{ig_mod._API_BASE}/me").mock(return_value=httpx.Response(
            400, json={"error": {"message": "API access blocked.",
                                 "type": "OAuthException", "code": 200}}))
        c = IgClient(access_token="tok")
        with pytest.raises(IgAuthError):
            await c.validate_session()
        await c.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_expired_token_code_190_returns_none(self):
        self._mock_refresh()
        respx.get(f"{ig_mod._API_BASE}/me").mock(return_value=httpx.Response(
            400, json={"error": {"message": "Error validating access token: expired",
                                 "type": "OAuthException", "code": 190}}))
        c = IgClient(access_token="tok")
        assert await c.validate_session() is None
        await c.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_valid_token_returns_username(self):
        self._mock_refresh()
        respx.get(f"{ig_mod._API_BASE}/me").mock(return_value=httpx.Response(
            200, json={"user_id": "678", "username": "kithe_ig"}))
        c = IgClient(access_token="tok")
        assert await c.validate_session() == "kithe_ig"
        await c.close()
