"""A DeviantArt posting token decides where a post lands (3.32.0).

Found by asking whether FA's "valid session, unverified owner" hole existed
elsewhere. On DeviantArt it did, and it had already caused damage.

DA has **two** credentials and only one of them was ever checked:

- the **app token** (client-credentials) that polling uses — not tied to any
  user, and `validate_credentials` confirmed it against `/gallery/all`, a
  **public** listing. A pass meant "this app works and that username exists".
- the **per-user token** minted from `da_refresh_token`, which is what actually
  decides which account a post lands on. Nothing checked it at all.

Measured on the live install: the **default** DA account's stored refresh token
authorised a *different* account. One piece posted "as" the first account
landed on the second — silently, successfully, and recorded under the account
that did not receive it. That is the residue of the 3.21.0 bare-key incident,
where a non-default account's rotated token was written to the bare (default)
key: the cause was fixed, the credential never repaired.

⚠ Two traps this file pins, both of which cost real time to find:

1. **Header auth only.** `/user/whoami` rejects `?access_token=` with
   `401 invalid_token: "Expired oAuth2 token"` — a misleading error for the
   wrong transport, which sends an investigation straight back to token expiry.
   The same token returns 200 through `Authorization: Bearer` in the same
   second.
2. **Checking costs a credential.** DA issues single-use refresh tokens, so
   obtaining a posting token rotates the stored one. Every check must go
   through `_ensure_client`, the one path that persists the rotation to the
   right per-account key.
"""
from __future__ import annotations

import asyncio

import pytest


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code = payload, status
        self.text = text or str(payload)

    def json(self):
        return self._payload


def _client(monkeypatch, payload, status=200):
    from clients.da.client import DAClient
    c = DAClient(cookie="", target_user="SecondFur")
    seen = {}

    class _Http:
        async def get(self, url, **kw):
            seen["url"] = url
            seen["headers"] = kw.get("headers") or {}
            seen["params"] = kw.get("params") or {}
            return _Resp(payload, status)

    c._http = _Http()
    c.seen = seen
    return c


# ── whoami ───────────────────────────────────────────────────────────

def test_whoami_returns_the_account_a_token_authorises():
    c = _client(None, {"username": "ThirdFur", "userid": "UUID", "type": "regular"})
    who = asyncio.run(c.whoami("tok"))
    assert who["username"] == "ThirdFur"


def test_whoami_sends_the_token_as_a_header_not_a_query_parameter():
    """THE trap. As a query parameter DA answers `401 invalid_token: "Expired
    oAuth2 token"` — which reads as expiry and is not."""
    c = _client(None, {"username": "ThirdFur"})
    asyncio.run(c.whoami("tok"))
    assert c.seen["headers"].get("Authorization") == "Bearer tok"
    assert "access_token" not in c.seen["params"]


def test_whoami_is_none_on_a_rejection():
    c = _client(None, {"error": "invalid_token"}, status=401)
    assert asyncio.run(c.whoami("tok")) is None


def test_whoami_is_none_without_a_token():
    from clients.da.client import DAClient
    assert asyncio.run(DAClient(cookie="", target_user="x").whoami("")) is None


def test_a_payload_with_no_username_is_not_an_identity():
    c = _client(None, {"userid": "UUID", "type": "regular"})
    assert asyncio.run(c.whoami("tok")) is None


# ── the poster refuses to post as somebody else ──────────────────────

def _poster(owner, target="SecondFur", token="tok"):
    """A poster whose refresh already happened, with a known token owner."""
    from posting.platforms.deviantart import DeviantArtPoster
    import time

    p = DeviantArtPoster()
    p._access_token = token
    p._token_expires_at = time.time() + 3600
    p._token_owner = ""
    # A poster holding a live token also holds the fingerprint of the
    # credentials it was minted from (3.32.1). Without it the poster correctly
    # concludes the credentials changed under it and tries to refresh — which
    # is the whole point of that mechanism, and would make these fixtures
    # exercise the refresh path instead of the identity check.
    p._cred_fp = DeviantArtPoster._fingerprint("id", "secret", "refresh")

    class _FakeClient:
        def __init__(self):
            self.target_user = target

        async def whoami(self, tok):
            return {"username": owner} if owner else None

    p._client = _FakeClient()

    async def _creds(*a, **k):
        return {}

    # _ensure_client re-reads settings; short-circuit that half.
    p._resolve_creds = lambda *a, **k: {
        "da_client_id": "id", "da_client_secret": "secret",
        "da_refresh_token": "refresh", "da_target_user": target}
    return p


def test_a_mismatched_token_refuses_rather_than_posting():
    """THE regression. An upload to the wrong account cannot be taken back."""
    p = _poster(owner="ThirdFur", target="SecondFur")
    with pytest.raises(RuntimeError, match="posts as ThirdFur"):
        asyncio.run(p._ensure_client())


def test_the_refusal_says_which_account_and_what_to_do():
    p = _poster(owner="ThirdFur", target="SecondFur")
    with pytest.raises(RuntimeError) as e:
        asyncio.run(p._ensure_client())
    msg = str(e.value)
    assert "SecondFur" in msg and "ThirdFur" in msg
    assert "Authorise posting" in msg


def test_the_matching_token_posts_normally():
    p = _poster(owner="SecondFur", target="SecondFur")
    client, token = asyncio.run(p._ensure_client())
    assert token == "tok"
    assert p._token_owner == "SecondFur"


def test_case_differences_are_not_a_mismatch():
    """`SecondFur` vs `secondfur` is one account. A false mismatch would block
    posting on a stored-capitalisation difference."""
    p = _poster(owner="secondfur", target="SecondFur")
    assert asyncio.run(p._ensure_client())[1] == "tok"


def test_an_unreachable_whoami_does_not_block_posting():
    """A network blip must not be read as "wrong account" — failing closed here
    would take posting down whenever DA hiccups."""
    p = _poster(owner=None, target="SecondFur")
    assert asyncio.run(p._ensure_client())[1] == "tok"


def test_the_identity_is_checked_once_per_token_not_once_per_post():
    """Access tokens last an hour; a whoami per post would be a needless call
    on every upload."""
    p = _poster(owner="SecondFur", target="SecondFur")
    calls = []
    real = p._client.whoami

    async def _counting(tok):
        calls.append(tok)
        return await real(tok)

    p._client.whoami = _counting
    asyncio.run(p._ensure_client())
    asyncio.run(p._ensure_client())
    assert len(calls) == 1


# ── the reporting surface ────────────────────────────────────────────

def test_validate_session_reports_a_mismatch_in_the_shared_shape():
    p = _poster(owner="ThirdFur", target="SecondFur")
    r = asyncio.run(p.validate_session())
    assert r["ok"] is False
    assert r["logged_in"] is True
    assert "ThirdFur" in r["detail"]


def test_validate_session_reports_a_match():
    p = _poster(owner="SecondFur", target="SecondFur")
    r = asyncio.run(p.validate_session())
    assert r["ok"] is True and r["username"] == "SecondFur"


def test_an_unconfirmed_identity_is_not_reported_as_wrong():
    p = _poster(owner=None, target="SecondFur")
    r = asyncio.run(p.validate_session())
    assert r["ok"] is True
    assert "unconfirmed" in r["detail"]


def _src(rel):
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / rel).read_text(
        encoding="utf-8", errors="replace")


def test_test_login_covers_deviantart():
    body = _src("routes/settings_api.py")
    assert 'if platform == "da":' in body
    assert "poster.validate_session()" in body


def test_test_login_goes_through_the_poster_because_the_check_spends_a_token():
    """DA refresh tokens are single-use. Only `_ensure_client` persists the
    rotation to the right per-account key; any other route would burn it."""
    body = _src("routes/settings_api.py")
    da = body[body.index('if platform == "da":'):]
    da = da[:da.index('return {"status": "unsupported"')]
    assert "_get_poster" in da
    assert "oauth_refresh_token" not in da, \
        "refreshing here would spend the token outside the path that saves it"


# ── the cookie fallback no longer trusts a public page ───────────────

def test_the_cookie_check_asks_for_a_page_behind_auth():
    """It used to fetch `/{target_user}/gallery` — public — and return true on
    `data-userid`, which a gallery carries for its owner whether or not anyone
    is signed in. The FurAffinity `<figure>` bug, a second time."""
    body = _src("clients/da/client.py")
    fn = body[body.index("async def validate_cookies"):]
    fn = fn[:fn.index("\n    # ── Gallery Discovery")]
    assert "/settings/" in fn
    assert "data-userid" not in fn.split('"""')[2], \
        "the public-page marker is still being trusted in code"


def test_validate_credentials_says_it_does_not_cover_posting():
    """The two DA credentials are independent, and conflating them is how an
    account reported healthy while its posting token belonged to someone else."""
    body = _src("clients/da/client.py")
    fn = body[body.index("async def validate_credentials"):]
    doc = fn[:fn.index('"""', fn.index('"""') + 3)]
    assert "says nothing about posting" in doc
