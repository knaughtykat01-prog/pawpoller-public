"""FurryNetwork behind a Cloudflare challenge (4.32.1, backlog FNCF).

Measured 2026-09-21: every furrynetwork.com URL answered 403 with `cf-mitigated: challenge` and
the "Just a moment…" page, to any user agent, and the operator's own browser got the same page.
The old code read that as `FurryNetwork auth failed: HTTP 403`, which reads like a dead login and
sends the user to re-enter a token that was never the problem.

These tests pin the three things that make the difference: the challenge is recognised, it is its
own error with an actionable message, and it does NOT fall through to the password grant.
"""
from __future__ import annotations

import asyncio

import pytest

from clients.fn.client import (FnAuthError, FnChallengeError, FnClient, FnRecaptchaError,
                               _is_cf_challenge)

CHALLENGE_HTML = ('<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
                  '<meta name="robots" content="noindex,nofollow"></head><body></body></html>')


class FakeResponse:
    def __init__(self, status_code=403, headers=None, text=CHALLENGE_HTML, json_body=None):
        self.status_code = status_code
        self.headers = headers if headers is not None else {"content-type": "text/html"}
        self.text = text
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeHttp:
    """Stands in for the client's httpx.AsyncClient; records what it was asked for."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append(("post", url, kw.get("data")))
        return self.response

    async def get(self, url, **kw):
        self.calls.append(("get", url, kw.get("params")))
        return self.response


def _client(response) -> tuple[FnClient, FakeHttp]:
    c = FnClient(username="someone", password="hunter2", refresh_token="stale-token")
    http = FakeHttp(response)
    c._http = lambda: http               # type: ignore[assignment]
    return c, http


class TestRecognised:
    def test_the_header_alone_is_enough(self):
        assert _is_cf_challenge(FakeResponse(headers={"cf-mitigated": "challenge"}, text=""))

    def test_header_match_is_case_insensitive(self):
        assert _is_cf_challenge(FakeResponse(headers={"CF-Mitigated": "challenge"}, text=""))

    def test_the_interstitial_html_is_enough(self):
        assert _is_cf_challenge(FakeResponse(headers={"content-type": "text/html"}))

    def test_503_counts_too(self):
        assert _is_cf_challenge(FakeResponse(status_code=503, headers={"cf-mitigated": "challenge"}))

    def test_a_normal_403_is_not_a_challenge(self):
        """An ordinary refusal must keep its own error, or we would tell the user the site is
        down every time a token is genuinely rejected."""
        assert not _is_cf_challenge(FakeResponse(
            headers={"content-type": "application/json"}, text='{"error":"invalid_grant"}',
            json_body={"error": "invalid_grant"}))

    def test_a_200_is_never_a_challenge(self):
        assert not _is_cf_challenge(FakeResponse(status_code=200, text=CHALLENGE_HTML))


class TestTokenRequest:
    def test_challenge_raises_its_own_error(self):
        c, _ = _client(FakeResponse(headers={"cf-mitigated": "challenge"}))
        with pytest.raises(FnChallengeError):
            asyncio.run(c._token_request({"grant_type": "refresh_token", "refresh_token": "x"}))

    def test_the_message_says_it_is_the_site_not_the_login(self):
        c, _ = _client(FakeResponse(headers={"cf-mitigated": "challenge"}))
        with pytest.raises(FnChallengeError) as e:
            asyncio.run(c._token_request({"grant_type": "refresh_token", "refresh_token": "x"}))
        msg = str(e.value).lower()
        assert "cloudflare" in msg
        assert "not your login" in msg
        assert "http 403" not in msg          # the old, misleading text

    def test_still_an_fn_auth_error_for_existing_handlers(self):
        c, _ = _client(FakeResponse(headers={"cf-mitigated": "challenge"}))
        with pytest.raises(FnAuthError):
            asyncio.run(c._token_request({"grant_type": "refresh_token", "refresh_token": "x"}))

    def test_recaptcha_is_still_its_own_error(self):
        """The August failure mode (password grant behind reCAPTCHA, 422) must not be swallowed
        by the new branch."""
        c, _ = _client(FakeResponse(status_code=422, headers={"content-type": "application/json"},
                                    text='{"message": "Invalid Recaptcha Token"}',
                                    json_body={"message": "Invalid Recaptcha Token"}))
        with pytest.raises(FnRecaptchaError):
            asyncio.run(c._token_request({"grant_type": "password", "username": "a", "password": "b"}))


class TestLogin:
    def test_a_challenge_does_not_fall_back_to_the_password_grant(self):
        """One doomed request, not two — and the fallback would relabel a site outage as a dead
        token."""
        c, http = _client(FakeResponse(headers={"cf-mitigated": "challenge"}))
        with pytest.raises(FnChallengeError):
            asyncio.run(c.login())
        assert len(http.calls) == 1
        assert http.calls[0][2]["grant_type"] == "refresh_token"

    def test_a_dead_refresh_token_still_falls_back(self):
        c, http = _client(FakeResponse(status_code=400, headers={"content-type": "application/json"},
                                       text='{"error":"invalid_grant"}',
                                       json_body={"error": "invalid_grant"}))
        with pytest.raises(FnAuthError):
            asyncio.run(c.login())
        assert [call[2]["grant_type"] for call in http.calls] == ["refresh_token", "password"]


class TestReads:
    def test_a_read_behind_the_challenge_raises_rather_than_returning_none(self):
        """Silently returning None made a poll report "no submissions" instead of "unreachable"."""
        c, _ = _client(FakeResponse(headers={"cf-mitigated": "challenge"}))
        c.access_token = "live-token"
        c._token_expiry = float("inf")
        with pytest.raises(FnChallengeError):
            asyncio.run(c._get("user"))
