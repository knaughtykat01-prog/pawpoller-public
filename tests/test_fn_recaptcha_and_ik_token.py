"""Two live-platform breakages found in prod logs on 2026-08-19.

Both are failures where the platform's own error text points at the wrong
thing, which is why each gets a test naming what actually happened.
"""
from __future__ import annotations

import httpx
import pytest

from clients.fn.client import FnAuthError, FnClient, FnRecaptchaError
from clients.ik.client import _auth_header


# ── Itaku: the 401 that blames the token ──────────────────────

def test_a_token_pasted_with_its_prefix_still_builds_one_valid_header():
    """Prod, posting `Showing_Off`: 401 "Token string should not contain spaces."

    Django REST Framework splits the auth header on whitespace and rejects
    anything that is not exactly two parts. Pasting `Token abc` out of DevTools
    makes three.
    """
    assert _auth_header("Token abc123") == {"Authorization": "Token abc123"}
    assert _auth_header("token abc123") == {"Authorization": "Token abc123"}


def test_surrounding_whitespace_is_trimmed():
    assert _auth_header("  abc123\n") == {"Authorization": "Token abc123"}


def test_a_clean_token_is_untouched():
    assert _auth_header("abc123") == {"Authorization": "Token abc123"}


def test_the_header_never_has_more_than_two_parts():
    """The property DRF actually enforces, asserted directly."""
    for raw in ("abc123", " abc123 ", "Token abc123", "\tToken  abc123\n", ""):
        assert len(_auth_header(raw)["Authorization"].split()) <= 2


# ── FurryNetwork: the 422 that said "HTTP 422" ────────────────

class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _client_with_response(monkeypatch, status_code, payload):
    client = FnClient(username="a@b.c", password="pw")

    class _FakeHttp:
        async def post(self, *a, **kw):
            return _FakeResponse(status_code, payload)

    monkeypatch.setattr(client, "_http", lambda: _FakeHttp())
    return client


@pytest.mark.asyncio
async def test_the_recaptcha_rejection_is_named_not_reported_as_http_422(monkeypatch):
    """FN answers middleware rejections as {"message": ...}, not the OAuth
    {"error_description": ...}. Reading only the OAuth keys turned the single
    most important failure into the useless string "HTTP 422"."""
    client = _client_with_response(monkeypatch, 422, {"message": "Invalid Recaptcha Token"})
    with pytest.raises(FnRecaptchaError) as exc:
        await client._token_request({"grant_type": "password"})
    assert "reCAPTCHA" in str(exc.value)
    assert "refresh token" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_recaptcha_error_is_still_caught_as_an_auth_error(monkeypatch):
    """Every existing `except FnAuthError` must keep working."""
    client = _client_with_response(monkeypatch, 422, {"message": "Invalid Recaptcha Token"})
    with pytest.raises(FnAuthError):
        await client._token_request({"grant_type": "password"})


@pytest.mark.asyncio
async def test_an_ordinary_oauth_failure_still_reads_normally(monkeypatch):
    client = _client_with_response(
        monkeypatch, 400,
        {"error": "invalid_grant", "error_description": "Invalid refresh token"})
    with pytest.raises(FnAuthError) as exc:
        await client._token_request({"grant_type": "refresh_token"})
    assert "Invalid refresh token" in str(exc.value)
    assert not isinstance(exc.value, FnRecaptchaError)


@pytest.mark.asyncio
async def test_a_message_only_error_is_surfaced_rather_than_swallowed(monkeypatch):
    """Any other middleware rejection should also read as itself."""
    client = _client_with_response(monkeypatch, 422, {"message": "Account suspended"})
    with pytest.raises(FnAuthError) as exc:
        await client._token_request({"grant_type": "password"})
    assert "Account suspended" in str(exc.value)


@pytest.mark.asyncio
async def test_login_prefers_the_refresh_token_over_the_dead_password_grant(monkeypatch):
    """With the password grant behind reCAPTCHA, the refresh token is the only
    credential that can work — so it must be tried first."""
    client = FnClient(username="a@b.c", password="pw", refresh_token="rt-1")
    grants = []

    async def fake_token_request(data):
        grants.append(data["grant_type"])
        client.refresh_token = "rt-2"
        return {}

    monkeypatch.setattr(client, "_token_request", fake_token_request)
    assert await client.login() is True
    assert grants == ["refresh_token"]
    assert client.refresh_token == "rt-2", "a rotated token must be kept"
